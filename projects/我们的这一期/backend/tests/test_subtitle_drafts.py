from copy import deepcopy
from app.models import Episode, Job
from app.services.dialogue_captions import draft_rows_from_steps
from test_pipeline import (client, settings, services, no_network, voiced_video_bytes,
    create_with_asset, request, episode_state, advance)


def seed(client, eid, aid):
    with client.app.state.repo.transaction() as db:
        ep=db.get(Episode,eid)
        source=Job(owner_id=ep.owner_id,episode_id=eid,kind='asr_trial',state='succeeded',
            idempotency_key='asr-draft-test',payload={'revision':ep.evidence_revision,'asset_ids':[aid]})
        db.add(source);db.flush()
        draft=Job(owner_id=ep.owner_id,episode_id=eid,kind='subtitle_draft',state='succeeded',
            idempotency_key='subtitle-draft-test',payload={'revision':ep.evidence_revision,'source_job_id':source.id},
            result={'rows':[{'asset_id':aid,'start':.2,'end':1.3,'text':'先别急'}]})
        db.add(draft);db.flush()
        return source.id,draft.id


def version(client,auth,eid,aid):
    return request(client,f'/episodes/{eid}/versions',auth,expected=201,
        body={'title':'字幕草稿','packaging':'variety_v2',
              'segments':[{'asset_id':aid,'start':0.,'end':2.,'caption':''}]})


def test_draft_edit_render_preserves_unconfirmed_evidence(client,services,voiced_video_bytes):
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    seed(client,eid,asset['id']);base=version(client,auth,eid,asset['id'])
    before=deepcopy(services.counts)
    track=request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')
    cue=track['rows'][0]
    assert cue['text']=='先别急' and not cue['reviewed']
    assert '自动字幕草稿，尚未核对' in cue['flags']
    segments=deepcopy(base['segments']);segments[0]['subtitle_edits']=[dict(cue_id=cue['cue_id'],text='先别急！',reviewed=True)]
    edited=request(client,f'/episodes/{eid}/versions',auth,expected=201,
        body={'title':'修改草稿','base_version_id':base['id'],'packaging':'variety_v2','segments':segments})
    changed=request(client,f"/versions/{edited['id']}/subtitles",auth,method='GET')['rows'][0]
    assert changed['text']=='先别急！' and changed['reviewed']
    assert '自动字幕草稿，尚未核对' not in changed['flags']
    assert request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows'][0]['text']=='先别急'
    job=request(client,f"/versions/{edited['id']}/render",auth);advance(client,job['id'])
    assert next(v for v in episode_state(client,eid)['versions'] if v['id']==edited['id'])['state']=='ready'
    with client.app.state.repo.factory() as db:
        ep=db.get(Episode,eid);assert ep.evidence_confirmed_revision is None and ep.evidence==[]
    assert services.counts==before


def test_draft_rejects_other_owner_stale_failed_and_invalid_rows(client,voiced_video_bytes):
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    sid,did=seed(client,eid,asset['id']);base=version(client,auth,eid,asset['id'])
    def rows():return request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']
    for field,value in [('owner_id','other'),('state','failed')]:
        with client.app.state.repo.transaction() as db:
            obj=db.get(Job,sid);old=getattr(obj,field);setattr(obj,field,value)
        assert not rows()
        with client.app.state.repo.transaction() as db:setattr(db.get(Job,sid),field,old)
    with client.app.state.repo.transaction() as db:
        draft=db.get(Job,did);draft.payload={**draft.payload,'revision':999}
    assert not rows()
    with client.app.state.repo.transaction() as db:
        draft=db.get(Job,did);draft.payload={**draft.payload,'revision':db.get(Episode,eid).evidence_revision}
        draft.result={'rows':[{'asset_id':asset['id'],'start':-2.,'end':1.,'text':'invalid'},
                             {'asset_id':asset['id'],'start':.1,'end':99.,'text':'outside'},
                             {'asset_id':'another','start':.1,'end':1.,'text':'wrong asset'}]}
    assert not rows()


def test_later_draft_does_not_replace_saved_caption_baseline(client,voiced_video_bytes):
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    sid,did=seed(client,eid,asset['id']);base=version(client,auth,eid,asset['id'])
    before=request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']
    with client.app.state.repo.transaction() as db:
        first=db.get(Job,did)
        db.add(Job(owner_id=first.owner_id,episode_id=eid,kind='subtitle_draft',state='succeeded',
            idempotency_key='newer-subtitle-draft',payload=deepcopy(first.payload),
            result={'rows':[{'asset_id':asset['id'],'start':.2,'end':1.3,'text':'新的识别内容'}]}))
    assert request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']==before


def test_normal_analysis_exposes_draft_before_evidence_confirmation(client,services,voiced_video_bytes):
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    analysis=request(client,f'/episodes/{eid}/analysis',auth)
    advance(client,analysis['id'])
    before=deepcopy(services.counts)
    base=version(client,auth,eid,asset['id'])
    track=request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']
    assert len(track)==1 and track[0]['text']=='测试替身转写，请人工核对'
    assert not track[0]['reviewed'] and '自动字幕草稿，尚未核对' in track[0]['flags']
    with client.app.state.repo.factory() as db:
        ep=db.get(Episode,eid);job=db.get(Job,analysis['id'])
        assert ep.evidence_confirmed_revision is None
        assert job.result['revision']==ep.evidence_revision!=job.payload['revision']
    # An explicit later draft must not silently replace the analysis baseline.
    seed(client,eid,asset['id'])
    assert request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']==track
    assert services.counts==before


def test_validation_or_stale_analysis_cannot_supply_draft(client,voiced_video_bytes):
    auth,eid,asset=create_with_asset(client,voiced_video_bytes)
    analysis=request(client,f'/episodes/{eid}/analysis',auth);advance(client,analysis['id'])
    base=version(client,auth,eid,asset['id'])
    with client.app.state.repo.transaction() as db:
        job=db.get(Job,analysis['id']);job.payload={**job.payload,'validation_only':True}
    assert request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']==[]
    with client.app.state.repo.transaction() as db:
        job=db.get(Job,analysis['id']);job.payload={**job.payload,'validation_only':False}
        job.result={**job.result,'revision':0}
    assert request(client,f"/versions/{base['id']}/subtitles",auth,method='GET')['rows']==[]


def test_grouping_respects_speaker_scope_overlap_punctuation_and_gaps():
    def w(a,b,t,s='S1',scope='a:0'):
        return dict(asset_id='a',start=a,end=b,text=t,speaker=s,speaker_scope=scope)
    words=[w(0.,.2,'先'),w(.2,.4,'别急'),w(.4,.7,'我来','S2'),
           w(.7,1.,'试试','S2'),w(.9,1.2,'等等'),w(1.2,1.5,'好？'),
           w(1.5,1.7,'嗯'),w(2.5,2.8,'来'),w(2.8,3.,'了',scope='a:30')]
    rows=draft_rows_from_steps({'asr:a':words},['a'])
    assert [r['text'] for r in rows]==['先别急','我来试试','等等好？','嗯','来','了']
    assert [(r['start'],r['end']) for r in rows]==[(0.,.4),(.4,1.),(.9,1.5),(1.5,1.7),(2.5,2.8),(2.8,3.)]


def test_grouping_retains_english_boundaries_and_long_unaligned_utterances():
    words=[dict(asset_id='a',start=0.,end=.2,text='Hello'),
           dict(asset_id='a',start=.2,end=.5,text='world!'),
           dict(asset_id='a',start=.5,end=4.,text='这是一整句没有逐字时间的转写不能猜出每个字的时间。')]
    rows=draft_rows_from_steps({'asr:a':words},['a'])
    assert rows[0]['text']=='Hello world!'
    assert rows[1]['text']==words[2]['text'] and rows[1]['end']==4.
    assert len(rows)==2
