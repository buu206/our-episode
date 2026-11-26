"""Accepted changes use a bounded local delegation, never a creative approval."""
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.models import Version,Job
from app.services.director_run import find, REVISION_SCOPE
from app.services.pipeline import Pipeline
from app.services.media import MediaError
from app.worker import Worker
from test_director_cut import proposal as cut_proposal, cut_version
from test_director_subtitles import proposal as subtitle_proposal
from test_audio_review import audio_version, transport, rendered
from test_visual_review import client
from test_workflow_access import post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS

@pytest.fixture(params=['cut_proposal','subtitle_proposal'])
def accepted(request):
    data=request.getfixturevalue(request.param)
    c,repo,grant,vid,path,pid,services=data
    url=f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{pid}'
    response=c.post(url,json={'confirm':True});assert response.status_code==200,response.text
    result=response.json();assert c.post(url,json={'confirm':True}).json()==result
    with repo.factory() as db:
        new=db.get(Version,result['version_id']);run=find(db,new)
        assert run.state=='active' and run.result['scope']==REVISION_SCOPE
        assert run.payload['approved_proposal_id']==pid
        assert len(list(db.scalars(select(Job).where(Job.kind=='director_run',Job.version_id==new.id))))==1
    return data,result

def control(accepted,action):
    data,result=accepted
    return data[0].post(f'/api/v1/versions/{result["version_id"]}/director-run/{action}')

def runner(accepted):
    data,_=accepted;return Worker(data[1],Pipeline(data[1],data[-1]))

def test_pause_restart_resume_render_and_check(accepted):
    data,result=accepted;c,repo,grant,base,path,pid,services=data;calls=deepcopy(services.counts)
    assert control(accepted,'pause').json()['state']=='paused'
    runner(accepted).process_one()
    assert c.post(f'/api/v1/versions/{result["version_id"]}/render').status_code==409
    repo.recover()
    assert control(accepted,'resume').json()['state']=='active'
    runner(accepted).process_one()
    with repo.factory() as db:
        v=db.get(Version,result['version_id']);assert find(db,v).payload['phase']=='technical_review'
    assert control(accepted,'pause').json()['state']=='paused'
    runner(accepted).process_one()
    assert control(accepted,'resume').status_code==200
    runner(accepted).process_one()
    with repo.factory() as db:
        v=db.get(Version,result['version_id']);r=find(db,v)
        assert r.state=='awaiting_user' and '修改版' in r.stage
        assert v.state=='ready' and v.confirmed_at is None
        assert db.get(Version,base).state=='ready'
    assert services.counts==calls
    assert c.get(f'/api/v1/versions/{result["version_id"]}/export').status_code==409

@pytest.mark.parametrize('inflight',[False,True])
def test_cancel_preserves_old_and_never_starts_followup(accepted,monkeypatch,inflight):
    from app.services import variety_v2
    data,result=accepted;c,repo,grant,base,path,pid,services=data
    original=variety_v2.render_variety
    if inflight:
        def rendering(*args,**kwargs):
            assert control(accepted,'cancel').json()['state']=='cancelling'
            return original(*args,**kwargs)
        monkeypatch.setattr(variety_v2,'render_variety',rendering)
    else:assert control(accepted,'cancel').json()['state']=='cancelled'
    runner(accepted).process_one()
    with repo.factory() as db:
        v=db.get(Version,result['version_id']);assert find(db,v).state=='cancelled'
        assert v.state==('ready' if inflight else 'draft')
        assert db.get(Version,base).state=='ready'
        assert not db.scalar(select(Job).where(Job.kind=='technical_review',Job.version_id==v.id))
    assert control(accepted,'resume').status_code==409


def test_retry_bound_and_no_new_model_calls(accepted,monkeypatch):
    data,result=accepted;c,repo,grant,base,path,pid,services=data;calls=deepcopy(services.counts);renders=[]
    def fail(*args,**kwargs):
        renders.append(1);raise MediaError('MEDIA_TIMEOUT','合成超时')
    monkeypatch.setattr('app.services.variety_v2.render_variety',fail)
    runner(accepted).process_one();runner(accepted).process_one();runner(accepted).process_one()
    with repo.factory() as db:
        v=db.get(Version,result['version_id']);r=find(db,v)
        assert r.state=='blocked' and r.payload['retries_used']==1
        assert db.get(Version,base).state=='ready'
    assert renders==[1,1] and calls==services.counts
    assert control(accepted,'resume').status_code==409


def test_revision_delivery_keeps_old_review_separate_and_exports_exact_candidate(accepted):
    """Local synthetic delivery after an approved card; no real user confirmation."""
    import json
    import subprocess
    from app.models import ModelCall
    from app.services import audio_review

    data,result=accepted
    c,repo,grant,base,path,pid,services=data
    before=path.read_bytes()
    calls=deepcopy(services.counts)
    with repo.factory() as db:
        ledger_before=len(list(db.scalars(select(ModelCall))))
    worker=runner(accepted)
    worker.process_one()  # Actual FFmpeg render.
    worker.process_one()  # Local technical review.
    vid=result['version_id']
    with repo.factory() as db:
        v=db.get(Version,vid)
        assert find(db,v).state=='awaiting_user'
        assert v.state=='ready' and v.confirmed_at is None
        assert audio_review.context(repo,db,v) is None  # Old ASR is not a new review.
        output=repo.path(v.output_path)
        expected=output.read_bytes()
        saved_segments=deepcopy(v.segments)
    streams=json.loads(subprocess.run(
        ['ffprobe','-v','error','-show_streams','-of','json',str(output)],
        check=True,capture_output=True,text=True,timeout=20).stdout)['streams']
    assert {stream['codec_type'] for stream in streams}>={'video','audio'}
    assert float(next(s for s in streams if s['codec_type']=='video')['duration'])>0
    assert c.get(f'/api/v1/versions/{vid}/export').status_code==409
    post(c,f'/versions/{vid}/confirm')  # Only this isolated test candidate.
    first=c.get(f'/api/v1/versions/{vid}/export')
    assert first.status_code==200 and first.content==expected
    repo.recover()
    assert c.get(f'/api/v1/versions/{vid}/export').content==expected
    # Lost-response retry must not render, charge, or create another version.
    assert c.post(f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{pid}',
                  json={'confirm':True}).json()==result
    with repo.factory() as db:
        v=db.get(Version,vid)
        assert v.state=='confirmed' and v.confirmed_at is not None
        assert v.segments==saved_segments
        assert db.get(Version,base).confirmed_at is None
        assert len(list(db.scalars(select(Version))))==2
        assert len(list(db.scalars(select(ModelCall))))==ledger_before
    assert path.read_bytes()==before and services.counts==calls
    assert c.get(f'/api/v1/versions/{base}/export').status_code==409
    login=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json()
    c.headers['X-CSRF-Token']=login['csrf_token']
    assert c.get(f'/api/v1/versions/{vid}/export').status_code==404
