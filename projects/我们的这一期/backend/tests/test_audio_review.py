"""Actual media with simulated HTTP; not a real-model smoke."""
import httpx
import pytest
from sqlalchemy import select
from app.models import Asset, Budget, Episode, Job, ModelCall, Version
from app.services import audio_review as ar
from app.repository import DomainError
from app.services.pipeline import Pipeline, ModelServices
from app.services.material_trial import GRANT_FILE
from app.worker import Worker
from test_workflow_access import client, post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS
from test_visual_review import rendered
from test_minimax_asr import envelope

@pytest.fixture
def audio_version(rendered):
    c, repo, grant, vid, path = rendered
    with repo.transaction() as db:
        v=db.get(Version,vid);v.packaging='variety_v2';v.evidence_revision=5
        ep=db.get(Episode,grant['episode_id'])
        ep.evidence=[*ep.evidence,dict(id='speech',asset_id=grant['assets'][0]['id'],kind='speech',start=.1,end=.9,note='您好！')]
    return rendered

@pytest.fixture
def transport(monkeypatch):
    state={'calls':0,'mode':'valid','during':None};original=httpx.AsyncClient
    def handler(request):
        state['calls']+=1
        assert str(request.url)=='https://api.minimax.cn/v1/speech_to_text'
        assert '您好'.encode() not in request.content
        if state['mode']=='timeout':raise httpx.ReadTimeout('private upstream text')
        data=envelope()
        if state['mode']=='invalid':data['segments'][0]['end']=0
        if state['during']:state['during']()
        return httpx.Response(200,json=data)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    return state

def run(repo):Worker(repo,Pipeline(repo,ModelServices(KEYS))).process_one()

def test_audio_comparison_budget_idempotency_and_restart(audio_version,transport):
    c,repo,grant,vid,path=audio_version
    j=post(c,f'/versions/{vid}/audio-review')
    assert post(c,f'/versions/{vid}/audio-review')['id']==j['id']
    run(repo)
    with repo.factory() as db:
        job=db.get(Job,j['id']);v=db.get(Version,vid)
        assert job.state=='succeeded',job.error
        assert v.state=='ready' and v.confirmed_at is None
        report=job.result
        assert report['scope']=='output_audio_asr' and report['verification']=='unverified'
        assert report['captions_checked']==1 and report['issue_count']==1
        assert report['findings'][0]['caption']=='您好！'
        assert report['findings'][0]['recognized']=='你好！'
        assert ar.context(repo,db,v)['issue_count']==1
        assert not job.payload.get('artifacts')
        call=db.scalars(select(ModelCall)).one()
        assert call.kind=='asr' and db.get(Budget,call.reservation_id).state=='settled'
    repo.recover()
    assert post(c,f'/versions/{vid}/audio-review')['id']==j['id']
    assert transport['calls']==1 and not list((repo.data_dir/'incoming').glob('work-*'))
    assert c.get(f'/api/v1/versions/{vid}/export').status_code==409
    path.write_bytes(path.read_bytes()+b'changed')
    with repo.factory() as db:assert ar.context(repo,db,db.get(Version,vid)) is None

@pytest.mark.parametrize('change',['output','source','grant','captions','owner'])
def test_changed_snapshot_blocks_before_send(audio_version,transport,change):
    c,repo,grant,vid,path=audio_version;j=post(c,f'/versions/{vid}/audio-review')
    if change=='output':path.write_bytes(path.read_bytes()+b'changed')
    elif change=='source':(repo.data_dir/grant['assets'][0]['path']).write_bytes(b'changed')
    elif change=='grant':(repo.data_dir/GRANT_FILE).unlink()
    else:
        with repo.transaction() as db:
            if change=='owner':db.get(Version,vid).owner_id='other'
            else:
                ep=db.get(Episode,grant['episode_id']);ep.evidence=[{**row,'note':'新字幕'} if row['kind']=='speech' else row for row in ep.evidence]
    run(repo);assert transport['calls']==0
    with repo.factory() as db:
        assert db.get(Job,j['id']).state=='failed' and db.get(Version,vid).state=='ready'

@pytest.mark.parametrize('mode',['timeout','invalid','revoked'])
def test_failed_review_preserves_ready_video_and_never_retries(audio_version,transport,mode):
    c,repo,grant,vid,path=audio_version;j=post(c,f'/versions/{vid}/audio-review')
    if mode=='revoked':
        def revoke():
            with repo.transaction() as db:db.get(Asset,grant['assets'][0]['id']).deleted=True
        transport['during']=revoke
    else:transport['mode']=mode
    run(repo)
    with repo.factory() as db:
        job=db.get(Job,j['id']);assert job.state=='failed' and job.result is None
        assert db.get(Version,vid).state=='ready'
        call=db.scalars(select(ModelCall)).one()
        assert db.get(Budget,call.reservation_id).state==('unknown' if mode=='timeout' else 'settled')
    if mode!='revoked':assert post(c,f'/versions/{vid}/audio-review')['id']==j['id']
    assert transport['calls']==1

def test_authorization_and_budget(audio_version,transport):
    c,repo,grant,vid,path=audio_version;csrf=c.headers.pop('X-CSRF-Token')
    assert c.post(f'/api/v1/versions/{vid}/audio-review').status_code==403
    c.headers['X-CSRF-Token']=csrf
    with repo.transaction() as db:db.add(Budget(owner_id=grant['owner_id'],episode_id=grant['episode_id'],amount=3000000,cost=3000000,state='settled'))
    post(c,f'/versions/{vid}/audio-review');run(repo);assert transport['calls']==0
    auth=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json();c.headers['X-CSRF-Token']=auth['csrf_token']
    assert c.post(f'/api/v1/versions/{vid}/audio-review').status_code==404

def cue(text='你好！',start=.1,end=.9):return dict(cue_id='c',text=text,start=start,end=end)
def word(text='你好',start=.1,end=.9):return dict(text=text,start=start,end=end,asset_id='v')

def test_comparison_ignores_only_punctuation_not_fillers_or_different_words():
    assert ar.compare([cue('Hello，世界！')],[word('hello 世界')],2)['issue_count']==0
    assert ar.compare([cue('你好')],[word('嗯你好')],2)['issue_count']==1
    assert ar.compare([cue()],[],2)['findings'][0]['code']=='ASR_NO_MATCH'
    assert ar.compare([cue()],[word('你好啊',0,1.5)],2)['findings'][0]['code']=='ASR_BOUNDARY_UNCERTAIN'

def test_many_differences_are_bounded_and_not_marked_confirmed():
    r=ar.compare([cue('错误',n,n+.5) for n in range(20)],[],30)
    assert r['issue_count']==20 and len(r['findings'])==8 and r['findings_omitted']==12 and r['verification']=='unverified'

@pytest.mark.parametrize('patch',[{'start':float('nan')},{'end':5},{'text':' '},{'asset_id':'other'},{'start':True}])
def test_cached_asr_results_are_validated(patch):
    with pytest.raises(DomainError):ar.checked_words([{**word(),**patch}],'v',2)

def test_director_gets_bounded_summary_without_mutating_saved_report(audio_version,transport):
    c,repo,grant,vid,path=audio_version;j=post(c,f'/versions/{vid}/audio-review');run(repo)
    with repo.transaction() as db:
        job=db.get(Job,j['id']);full=ar.compare([cue('不同',n,n+.5) for n in range(12)],[],30)
        job.result={**job.result,**full}
    with repo.factory() as db:
        summary=ar.context(repo,db,db.get(Version,vid))
        assert len(summary['findings'])==3 and summary['findings_omitted']==9
        assert summary['verification']=='unverified' and summary['issue_count']==12
        assert len(db.get(Job,j['id']).result['findings'])==8
        assert 'not_checked' in summary

def test_expected_caption_offset_uses_same_frame_rounding_as_renderer(audio_version,monkeypatch):
    from app.services import packaging
    c,repo,grant,vid,path=audio_version
    with repo.transaction() as db:
        v=db.get(Version,vid);base=ar.expected_captions(repo,db,v)
        v.narration_path='outputs/narration.wav'
        monkeypatch.setattr(packaging,'_narration',lambda p:(bytes(32000),16000,1))
        shifted=ar.expected_captions(repo,db,v)
        assert shifted[0]['start']-base[0]['start']==pytest.approx(1+2/30,abs=.001)
