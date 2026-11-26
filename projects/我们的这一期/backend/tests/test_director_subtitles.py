"""Synthetic media and simulated ASR/chat; never real model acceptance."""
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.models import Version, Episode, Job
from app.chat_models import DirectorChatTurn
from app.services import director_chat as chat
from app.services.pipeline import Pipeline
from app.worker import Worker
from test_audio_review import audio_version, rendered, transport, run
from test_visual_review import client
from test_workflow_access import post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS
from test_director_chat import ChatServices

@pytest.fixture
def proposal(audio_version,transport,request):
    c,repo,grant,vid,path=audio_version
    with repo.transaction() as db:
        v=db.get(Version,vid);v.segments=[{**v.segments[0],'end':1.}]
        if getattr(request, 'param', None) == 'voice':
            import wave
            voice=repo.data_dir/'outputs'/('f'*32+'.wav')
            with wave.open(str(voice),'wb') as f:
                f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\xe8\x03'*3200)
            v.narration_text='已确认开场';v.narration_path='outputs/'+'f'*32+'.wav';v.narration_approved=True
    post(c,f'/versions/{vid}/audio-review');run(repo)
    services=ChatServices();services.reply={'reply':'这里可以试一下这个字幕，先回听再决定。','action':{
        'kind':'revise_subtitles','title':'调整一句字幕','explanation':'对照声音后决定，保留原版。',
        'corrections':[{'finding':1,'text':'你好！'}]}}
    with repo.transaction() as db:
        ep=db.get(Episode,grant['episode_id'])
        job=chat.enqueue(repo,db,ep,services,client_id='subtitle-proposal-test',text='请建议具体改字',version_id=vid,step='preview',confirm_send=True);jid=job.id
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Job,jid).state=='succeeded',db.get(Job,jid).error
        turn=db.scalars(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid)).one();pid=turn.id
    return c,repo,grant,vid,path,pid,services

def apply(data,confirm=True):
    c,repo,grant,vid,path,pid,services=data
    return c.post(f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{pid}',json={'confirm':confirm})

def test_confirmed_changes_create_one_rendered_candidate_and_preserve_original(proposal):
    c,repo,grant,vid,path,pid,services=proposal;before=path.read_bytes()
    assert apply(proposal,False).status_code==409
    state=c.get(f'/api/v1/episodes/{grant["episode_id"]}/director/chat').json()
    p=state['proposals'][-1];assert p['subtitle_changes'][0]['before']=='您好！'
    assert p['subtitle_changes'][0]['after']=='你好！' and 'audio_snapshot' not in p
    result=apply(proposal);assert result.status_code==200,result.text
    result=result.json();assert apply(proposal).json()==result
    counts=deepcopy(services.counts);Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        v=db.get(Version,result['version_id']);base=db.get(Version,vid)
        assert v.state=='ready',v.error
        assert v.base_version_id==vid and v.confirmed_at is None
        assert base.state=='ready' and base.confirmed_at is None
        assert not base.segments[0].get('subtitle_edits')
        assert v.segments[0]['subtitle_edits'][0]['text']=='你好！'
    assert path.read_bytes()==before and services.counts==counts
    repo.recover();assert apply(proposal).json()==result
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==2

@pytest.mark.parametrize('change',['output','caption','grant'])
def test_stale_review_is_not_applied(proposal,change):
    c,repo,grant,vid,path,pid,services=proposal
    if change=='output':path.write_bytes(path.read_bytes()+b'changed')
    elif change=='grant':
        from app.services.material_trial import GRANT_FILE
        (repo.data_dir/GRANT_FILE).unlink()
    else:
        with repo.transaction() as db:
            ep=db.get(Episode,grant['episode_id']);ep.evidence=[{**r,'note':'改变'} if r['kind']=='speech' else r for r in ep.evidence]
    response=apply(proposal);assert response.status_code==409,response.text
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1

def test_no_tools_rolls_back_and_render_failure_keeps_old_version(proposal,monkeypatch):
    from app.services import director_subtitles as ds,media
    c,repo,grant,vid,path,pid,services=proposal
    with monkeypatch.context() as patch:
        patch.setattr(ds.shutil,'which',lambda _:None)
        assert apply(proposal).status_code==503
        with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1
    result=apply(proposal).json()
    with monkeypatch.context() as patch:
        def fail(*args,**kwargs):raise media.MediaError('TEST_RENDER_FAILURE','合成失败')
        patch.setattr('app.services.variety_v2.render_variety',fail)
        Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Version,vid).state=='ready'
        assert db.get(Version,result['version_id']).state=='failed'
    assert apply(proposal).json()==result

def test_missing_confirm_and_other_owner_cannot_apply(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    csrf=c.headers.pop('X-CSRF-Token');assert apply(proposal).status_code==403;c.headers['X-CSRF-Token']=csrf
    auth=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json();c.headers['X-CSRF-Token']=auth['csrf_token']
    assert apply(proposal).status_code==404

@pytest.mark.parametrize('proposal',['voice'],indirect=True)
def test_approved_voice_reused_without_tts(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    response=apply(proposal);assert response.status_code==200,response.text
    with repo.factory() as db:
        old=db.get(Version,vid);new=db.get(Version,response.json()['version_id'])
        assert new.narration_path==old.narration_path and new.narration_approved
        assert new.narration_text==old.narration_text
    assert services.counts['tts']==0

@pytest.mark.parametrize('mutation',['locked','duplicate'])
def test_protected_or_ambiguous_source_never_creates_candidate(proposal,monkeypatch,mutation):
    from app import worker
    c,repo,grant,vid,path,pid,services=proposal;original=worker.source_segments
    def changed(*args,**kwargs):
        result=deepcopy(original(*args,**kwargs))
        if kwargs.get('subtitle_review'):
            if mutation=='locked':result[0]['_preserve_expression']=True
            else:result[0]['_dialogue']*=2
        return result
    monkeypatch.setattr(worker,'source_segments',changed)
    assert apply(proposal).status_code==409
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1


def test_changed_audio_report_stops_chat_before_charge(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    with repo.transaction() as db:
        e=db.get(Episode,grant['episode_id'])
        job=chat.enqueue(repo,db,e,services,client_id='changed-report-test',text='再看看字幕',version_id=vid,step='preview',confirm_send=True);jid=job.id
    count=services.counts['director_chat'];path.write_bytes(path.read_bytes()+b'changed')
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:assert db.get(Job,jid).state=='failed'
    assert services.counts['director_chat']==count

@pytest.mark.parametrize('proposal',['voice'],indirect=True)
def test_missing_voice_marks_card_stale_without_breaking_conversation(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    with repo.factory() as db:voice=repo.path(db.get(Version,vid).narration_path)
    voice.unlink()
    response=c.get(f'/api/v1/episodes/{grant["episode_id"]}/director/chat')
    assert response.status_code==200,response.text
    assert response.json()['proposals'][-1]['status']=='stale'
    assert apply(proposal).status_code==409
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1
