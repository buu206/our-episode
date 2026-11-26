"""Synthetic media, simulated chat and real local FFmpeg, not model acceptance."""
from copy import deepcopy
import pytest
from sqlalchemy import select
from app.models import Version, Episode, Job, Asset
from app.chat_models import DirectorChatTurn
from app.services import director_chat as chat, director_cut as cut
from app.services.edit_contract import mark_edit
from app.services.pipeline import Pipeline
from app.worker import Worker
from test_audio_review import audio_version, rendered
from test_visual_review import client
from test_workflow_access import post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS
from test_director_chat import ChatServices

@pytest.fixture
def cut_version(audio_version):
    c,repo,grant,vid,path=audio_version
    with repo.transaction() as db:
        ep=db.get(Episode,grant['episode_id']);v=db.get(Version,vid);aid=v.segments[0]['asset_id']
        ep.evidence=[dict(id='visual',asset_id=aid,kind='visual',start=0.,end=2.,note='合成蓝色画面'),dict(id='first',asset_id=aid,kind='speech',start=.1,end=.9,note='你好'),dict(id='second',asset_id=aid,kind='speech',start=1.1,end=1.9,note='下一句')]
        v.segments=[dict(asset_id=aid,start=0.,end=2.,caption='',purpose='保留原话',evidence_ids=['visual'])]
        mark_edit(repo,db,v)
    return audio_version

@pytest.fixture
def proposal(cut_version):
    c,repo,grant,vid,path=cut_version;services=ChatServices()
    services.reply={'reply':'保留第一句话，另一句仍留在旧版。','action':{'kind':'revise_cut','title':'留下第一句','explanation':'原声缩短一半，第二句不进入新版。','selections':[dict(shot=1,start=0.,end=1.,reason='保留完整第一句')]}}
    with repo.transaction() as db:
        job=chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,client_id='cut-test',text='给具体剪法',version_id=vid,step='preview',confirm_send=True);jid=job.id
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Job,jid).state=='succeeded',db.get(Job,jid).error
        turn=db.scalar(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid));pid=turn.id
    return c,repo,grant,vid,path,pid,services

def apply(data,confirm=True):
    c,repo,grant,vid,path,pid,services=data
    return c.post(f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{pid}',json={'confirm':confirm})

def test_recut_explicit_idempotent_render_and_restart(proposal):
    c,repo,grant,vid,path,pid,services=proposal;before=path.read_bytes()
    assert apply(proposal,False).status_code==409
    card=c.get(f'/api/v1/episodes/{grant["episode_id"]}/director/chat').json()['proposals'][-1]
    assert card['before_seconds']==2 and card['after_seconds']==1
    assert 'selections' not in card and 'cut_binding' not in card
    result=apply(proposal);assert result.status_code==200,result.text
    result=result.json();assert apply(proposal).json()==result
    calls=deepcopy(services.counts);Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        new=db.get(Version,result['version_id']);old=db.get(Version,vid)
        assert new.state=='ready',new.error
        assert new.confirmed_at is None and new.base_version_id==vid
        assert new.segments[0]['end']==1 and old.segments[0]['end']==2
        assert old.state=='ready' and len(list(db.scalars(select(Version))))==2
        assert db.scalar(select(Job).where(Job.version_id==new.id,Job.kind=='technical_review'))
    assert path.read_bytes()==before and calls==services.counts
    repo.recover();assert apply(proposal).json()==result

@pytest.mark.parametrize('selections',[
    [dict(shot=1,start=.5,end=1.,reason='截断原话')],
    [dict(shot=9,start=0.,end=1.,reason='不存在')],
    [dict(shot=1,start=0.,end=3.,reason='越界')],
    [dict(shot=1,start=0.,end=1.,reason='第一句'),dict(shot=1,start=.5,end=1.,reason='重叠')],
    [dict(shot=1,start=1.,end=2.,reason='后面'),dict(shot=1,start=0.,end=1.,reason='前面')],
    [dict(shot=1,start=0.,end=2.,reason='没有改变')],
])
def test_invalid_cuts_rejected_before_new_candidate(cut_version,selections):
    from app.repository import DomainError
    c,repo,grant,vid,path=cut_version
    with repo.factory() as db:
        v=db.get(Version,vid)
        with pytest.raises(DomainError):cut.resolve(repo,db,v,selections,cut.binding(repo,db,v))
        assert len(list(db.scalars(select(Version))))==1

@pytest.mark.parametrize('mutation',['output','source','evidence','narration'])
def test_stale_proposal_rejected(proposal,mutation):
    c,repo,grant,vid,path,pid,services=proposal
    if mutation=='output':path.write_bytes(path.read_bytes()+b'changed')
    else:
        with repo.transaction() as db:
            v=db.get(Version,vid)
            if mutation=='narration':v.narration_text='新旁白'
            elif mutation=='evidence':db.get(Episode,grant['episode_id']).evidence_revision+=1
            else:repo.path(db.get(Asset,v.segments[0]['asset_id']).path).write_bytes(b'changed')
    assert apply(proposal).status_code==409
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1


def test_locked_interview_rejected(cut_version,monkeypatch):
    from app import worker
    from app.repository import DomainError
    c,repo,grant,vid,path=cut_version
    with repo.factory() as db:
        v=db.get(Version,vid);bound=cut.binding(repo,db,v);original=worker.source_segments
        def protect(*args,**kwargs):
            rows=original(*args,**kwargs);rows[0]['_preserve_expression']=True;return rows
        monkeypatch.setattr(worker,'source_segments',protect)
        with pytest.raises(DomainError,match='采访'):cut.resolve(repo,db,v,[dict(shot=1,start=0.,end=1.,reason='调整')],bound)


def test_pending_call_stops_before_charge_if_source_changed(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    with repo.transaction() as db:
        job=chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,client_id='cut-changed',text='再建议',version_id=vid,step='preview',confirm_send=True);jid=job.id
    count=services.counts['director_chat'];path.write_bytes(path.read_bytes()+b'changed')
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:assert db.get(Job,jid).state=='failed'
    assert services.counts['director_chat']==count


def test_other_owner_and_csrf(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    token=c.headers.pop('X-CSRF-Token');assert apply(proposal).status_code==403;c.headers['X-CSRF-Token']=token
    auth=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json();c.headers['X-CSRF-Token']=auth['csrf_token']
    assert apply(proposal).status_code==404


def test_removed_ranges_are_explicit_and_reviewed_subtitles_stay(cut_version):
    from app.worker import source_segments
    c,repo,grant,vid,path=cut_version
    with repo.transaction() as db:
        v=db.get(Version,vid);rows=source_segments(repo,db,v,subtitle_review=True)[0]['_dialogue']
        v.segments=[{**v.segments[0],'subtitle_edits':[dict(cue_id=r['cue_id'],text=r['text']+'！',hidden=False,reviewed=True) for r in rows]}]
    with repo.factory() as db:
        v=db.get(Version,vid);segments,public=cut.resolve(repo,db,v,[dict(shot=1,start=0.,end=1.,reason='第一句')],cut.binding(repo,db,v))
        assert len(segments[0]['subtitle_edits'])==1
        assert segments[0]['subtitle_edits'][0]['text']=='你好！'
        assert public['cut_changes'][-1]['removed']
        assert public['cut_changes'][-1]['start']==1 and public['cut_changes'][-1]['end']==2


def test_render_failure_and_missing_tool_preserve_base(proposal,monkeypatch):
    from app.services import media
    c,repo,grant,vid,path,pid,services=proposal
    with monkeypatch.context() as patch:
        patch.setattr(cut.shutil,'which',lambda _:None)
        assert apply(proposal).status_code==503
        with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1
    result=apply(proposal).json()
    def fail(*args,**kwargs):raise media.MediaError('TEST_FAILED','合成失败')
    monkeypatch.setattr('app.services.variety_v2.render_variety',fail)
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Version,result['version_id']).state=='failed'
        assert db.get(Version,vid).state=='ready'
    assert apply(proposal).json()==result

@pytest.mark.parametrize('question,protect',[
    ('留住开头的交代、现场那句“下一句”和结尾，先给卡不执行。',True),
    ('保留“下，一句！”',True),
    ('别删“下一句”',True),
    ('删掉“下一句”',False),
    ('不用保留“下一句”',False),
    ('是否保留“下一句”？',False),
    ('如果保留“下一句”会怎样？',False),
    ('保留开头；删掉“下一句”',False),
    ('保留“没有确认过的话”',False),
])
def test_explicit_quoted_retention_blocks_whole_utterance_loss(cut_version,question,protect):
    from app.repository import DomainError
    c,repo,grant,vid,path=cut_version
    with repo.factory() as db:
        version=db.get(Version,vid);ep=db.get(Episode,grant['episode_id'])
        context={'question':question,'confirmed_evidence':ep.evidence}
        args=(repo,db,version,[dict(shot=1,start=0.,end=1.,reason='只留首句')],cut.binding(repo,db,version),context)
        if protect:
            with pytest.raises(DomainError) as exc:cut.resolve(*args)
            assert exc.value.code=='REQUESTED_SPEECH_REMOVED'
        else:cut.resolve(*args)


def test_legacy_card_revalidated_before_display_and_apply(proposal):
    c,repo,grant,vid,path,pid,services=proposal
    with repo.transaction() as db:
        turn=db.get(DirectorChatTurn,pid)
        turn.context={**turn.context,'question':'请保留“下一句”，给个试剪卡'}
    card=c.get(f'/api/v1/episodes/{grant["episode_id"]}/director/chat').json()['proposals'][-1]
    assert card['status']=='stale'
    result=apply(proposal)
    assert result.status_code==409
    with repo.factory() as db:assert len(list(db.scalars(select(Version))))==1


def test_retention_checked_before_publishing_model_card(cut_version):
    c,repo,grant,vid,path=cut_version;services=ChatServices()
    services.reply={'reply':'保留你要的话','action':{'kind':'revise_cut','title':'试剪','explanation':'保留原话','selections':[dict(shot=1,start=0.,end=1.,reason='第一句')]}}
    with repo.transaction() as db:
        job=chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,client_id='protected-test',text='保留“下一句”，给个剪法',version_id=vid,step='preview',confirm_send=True);jid=job.id
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        job=db.get(Job,jid);turn=db.scalar(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid))
        assert job.state=='failed' and job.error['code']=='REQUESTED_SPEECH_REMOVED'
        assert turn.proposal is None and len(list(db.scalars(select(Version))))==1


def test_retention_constraints_sent_with_same_compacted_reference():
    import json
    from app.services.director_chat_model import prepare_chat_input
    eid='a'*32
    context={'question':'保留“下一句”，给试剪卡','confirmed_evidence':[{'id':eid,'kind':'speech','note':'下一句'}],'available_actions':['revise_cut']}
    payload,_=prepare_chat_input(context)
    sent=json.loads(payload)['untrusted_context']
    assert sent['protected_speech_evidence_ids']==[sent['confirmed_evidence'][0]['id']]
    assert eid not in payload and 'protected_speech_evidence_ids' not in context
