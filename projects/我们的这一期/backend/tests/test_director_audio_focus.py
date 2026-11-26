"""Specific persisted findings, never caller-provided transcript replacements."""
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.models import Episode,Job,Version
from app.chat_models import DirectorChatTurn
from app.repository import DomainError
from app.services import director_chat as chat,audio_review
from app.services.pipeline import Pipeline
from app.worker import Worker
from test_director_subtitles import proposal
from test_audio_review import audio_version,rendered,transport,run
from test_visual_review import client
from test_workflow_access import post,grant_file
from test_authorized_materials import setup,movie,confirmed,offline,KEYS

@pytest.fixture
def focused(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    with repo.transaction() as db:
        j=db.scalars(select(Job).where(Job.kind=='audio_review',Job.version_id==vid)).one()
        r=deepcopy(j.result);target=r['findings'][0]
        r['findings']=[{**target,'cue_id':ch*32,'caption':'其他检查项'} for ch in ('d','e','f')]+[target]
        r['issue_count']=4;r['findings_omitted']=0;j.result=r
    return proposal,target['cue_id']

def enqueue(focused,focus=None,client_id='focus-cue-test-01'):
    data,cue=focused;c,repo,grant,vid,path,pid,services=data
    with repo.transaction() as db:
        job=chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,client_id=client_id,
            text='看看这句，先给建议',version_id=vid,step='preview',confirm_send=True,audio_focus_cue_id=focus or cue)
        return job.id

def test_later_finding_is_selected_and_only_correct_cue_changes(focused):
    data,cue=focused;c,repo,grant,vid,path,pid,services=data
    jid=enqueue(focused)
    assert enqueue(focused)==jid
    with repo.factory() as db:
        t=db.scalars(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid)).one()
        check=t.context['audio_check'];assert check['focus_cue_id']==cue
        assert check['findings'][0]['cue_id']==cue and len(check['findings'])==3 and check['findings_omitted']==1
        assert audio_review.context(repo,db,db.get(Version,vid))['findings'][0]['cue_id']!=cue
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Job,jid).state=='succeeded',db.get(Job,jid).error
        t=db.scalars(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid)).one();tid=t.id
        assert t.proposal['subtitle_changes'][0]['before']=='您好！'
    result=c.post(f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{tid}',json={'confirm':True})
    assert result.status_code==200,result.text
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        v=db.get(Version,result.json()['version_id']);assert v.state=='ready',v.error
        assert v.segments[0]['subtitle_edits']==[dict(cue_id=cue,text='你好！',hidden=False,reviewed=True)]
        assert not db.get(Version,vid).segments[0].get('subtitle_edits')

@pytest.mark.parametrize('when',['before_send','before_publish','before_apply'])
def test_changed_finding_stops_before_new_or_wrong_action(focused,when):
    data,cue=focused;c,repo,grant,vid,path,pid,services=data
    jid=enqueue(focused) if when!='before_send' else None
    if when=='before_apply':Worker(repo,Pipeline(repo,services)).process_one()
    with repo.transaction() as db:
        j=db.scalars(select(Job).where(Job.kind=='audio_review',Job.version_id==vid)).one()
        r=deepcopy(j.result);r['findings']=r['findings'][:-1];r['issue_count']=3;j.result=r
    if when=='before_send':
        with pytest.raises(DomainError,match='这句'):enqueue(focused)
    elif when=='before_publish':
        Worker(repo,Pipeline(repo,services)).process_one()
        with repo.factory() as db:assert db.get(Job,jid).state=='failed'
    else:
        with repo.factory() as db:t=db.scalars(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid)).one();tid=t.id
        assert c.post(f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{tid}',json={'confirm':True}).status_code==409
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1

def test_same_request_id_cannot_switch_finding(focused):
    enqueue(focused)
    with pytest.raises(DomainError) as exc:enqueue(focused,focus='d'*32)
    assert exc.value.code=='DIRECTOR_REQUEST_CONFLICT'

def test_api_rejects_arbitrary_focus_and_other_owner(focused):
    data,cue=focused;c,repo,grant,vid,path,pid,services=data
    url=f'/api/v1/episodes/{grant["episode_id"]}/director/chat'
    body=dict(client_id='focus-schema-test',text='先看这句',version_id=vid,confirm_send=True,audio_focus_cue_id='<script>')
    assert c.post(url,json=body).status_code==422
    body['audio_focus_cue_id']=cue
    auth=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json();c.headers['X-CSRF-Token']=auth['csrf_token']
    assert c.post(url,json=body).status_code==404

def test_review_change_during_model_call_prevents_publishing(focused):
    data,cue=focused;c,repo,grant,vid,path,pid,services=data
    jid=enqueue(focused)
    def changed():
        with repo.transaction() as db:
            j=db.scalars(select(Job).where(Job.kind=='audio_review',Job.version_id==vid)).one()
            r=deepcopy(j.result);r['findings'][-1]['recognized']='新的识别结果';j.result=r
    services.after_send=changed
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Job,jid).state=='failed'
        t=db.scalars(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid)).one()
        assert t.reply is None and t.proposal is None
