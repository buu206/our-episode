"""Real local extraction and simulated provider, never a real model verdict."""
import json
from copy import deepcopy
import httpx
import pytest
from sqlalchemy import select
from app.models import Version, Job, ModelCall, Episode
from app.chat_models import DirectorChatTurn
from app.services import sequence_review as sr, director_chat
from app.services.pipeline import Pipeline, ModelServices
from app.services.director import DirectorError
from app.worker import Worker
from test_visual_review import rendered
from test_workflow_access import client, post, grant_file
from test_authorized_materials import setup, movie, confirmed, offline, KEYS
from test_new_providers import envelope
from test_director_chat import ChatServices

@pytest.fixture
def transport(monkeypatch):
    calls=[];original=httpx.AsyncClient
    def handler(request):
        calls.append(request)
        assert request.url.host=='api.deepseek.com'
        content=json.loads(request.content)['messages'][0]['content']
        frames=[json.loads(c['text']) for c in content[1:] if c['type']=='text']
        assert 2 <= len(frames) <= 24
        assert all(1 < f['time'] < 3 for f in frames)
        assert len([c for c in content if c['type']=='image_url'])==len(frames)
        return httpx.Response(200,json=envelope({'summary':'抽样可见物体位置变化。','observations':[{
            'first_frame':frames[0]['frame_id'],'last_frame':frames[-1]['frame_id'],
            'visible_change':'后图物体位置更靠右。','uncertainty':'不能判断采样之间的动作。'}]}))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    return calls

def run(repo):Worker(repo,Pipeline(repo,ModelServices(KEYS))).process_one()

def submit(data):return post(data[0],f'/versions/{data[3]}/sequence-review',{'start':1,'end':3})

def test_sequence_scoped_persisted_non_destructive_and_idempotent(rendered,transport):
    c,repo,grant,vid,path=rendered;old=path.read_bytes();job=submit(rendered)
    assert submit(rendered)['id']==job['id']
    run(repo)
    with repo.factory() as db:
        j=db.get(Job,job['id']);assert j.state=='succeeded',j.error
        report=j.result
        assert report['scope']=='sampled_sequence' and report['verification']=='unverified'
        assert not report['audio_analyzed'] and report['window']=={'start':1.,'end':3.}
        assert report['observations'][0]['start']<report['observations'][0]['end']
        assert len(report['sample_times'])==9 and report['not_checked']
        assert report['sampling']['frame_count']==9 and not report['sampling']['continuous_video']
        assert sr.context(repo,db,db.get(Version,vid))['windows'][0]['summary']==report['summary']
        assert len(list(db.scalars(select(ModelCall))))==1
        _,ctx=director_chat._context(repo,db,db.get(Episode,grant['episode_id']),[],'这段有什么值得保留？',vid,'preview')
        assert ctx['sequence_check']['verification']=='unverified' and ctx['visual_check'] is None
        assert db.get(Version,vid).state=='ready' and db.get(Version,vid).confirmed_at is None
    repo.recover();assert submit(rendered)['id']==job['id'];assert len(transport)==1
    assert path.read_bytes()==old
    assert c.get(f'/api/v1/versions/{vid}/export').status_code==409
    path.write_bytes(old+b'changed')
    with repo.factory() as db:assert sr.context(repo,db,db.get(Version,vid)) is None

@pytest.mark.parametrize('body',[{'start':0,'end':25},{'start':2,'end':1},{'start':-1,'end':1},
                                 {'start':1,'end':90},{'start':0,'end':9},{'start':0,'end':1,'approve':True}])
def test_invalid_window_never_queues_or_charges(rendered,body):
    c,repo,grant,vid,path=rendered
    assert c.post(f'/api/v1/versions/{vid}/sequence-review',json=body).status_code==422
    with repo.factory() as db:
        assert not list(db.scalars(select(Job).where(Job.kind=='sequence_review')))
        assert not list(db.scalars(select(ModelCall)))

@pytest.mark.parametrize('mutation',['output','grant','prompt'])
def test_changed_source_stops_before_paid_send_and_preserves_candidate(rendered,transport,mutation,monkeypatch,tmp_path):
    c,repo,grant,vid,path=rendered;job=submit(rendered)
    if mutation=='output':path.write_bytes(path.read_bytes()+b'changed')
    elif mutation=='grant':
        from app.services.material_trial import GRANT_FILE
        (repo.data_dir/GRANT_FILE).unlink()
    else:
        prompt=tmp_path/'prompt.txt';prompt.write_text('changed');monkeypatch.setattr(sr,'PROMPT',prompt)
    run(repo)
    assert not transport
    with repo.factory() as db:
        assert db.get(Job,job['id']).state=='failed'
        assert db.get(Version,vid).state=='ready'
        assert not list(db.scalars(select(ModelCall)))

@pytest.mark.parametrize('pair',[('x','s2'),('s2','s1'),('s1','s1')])
def test_unknown_or_reversed_frame_pairs_are_rejected(pair):
    result={'summary':'观察','observations':[dict(first_frame=pair[0],last_frame=pair[1],visible_change='变化',uncertainty='未知')]}
    with pytest.raises(DirectorError):sr.parse(json.dumps(result),[dict(frame_id='s1',time=1),dict(frame_id='s2',time=2)])

def test_permissions(rendered):
    c,repo,grant,vid,path=rendered;url=f'/api/v1/versions/{vid}/sequence-review';body={'start':1,'end':3}
    csrf=c.headers.pop('X-CSRF-Token');assert c.post(url,json=body).status_code==403;c.headers['X-CSRF-Token']=csrf
    auth=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json();c.headers['X-CSRF-Token']=auth['csrf_token']
    assert c.post(url,json=body).status_code==404

def test_new_sequence_report_blocks_pending_chat(rendered,transport):
    c,repo,grant,vid,path=rendered;review=submit(rendered);run(repo);services=ChatServices()
    with repo.transaction() as db:
        j=director_chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,
            client_id='sequence-chat',text='先讨论',version_id=vid,step='preview',confirm_send=True);jid=j.id
        r=db.get(Job,review['id']);data=deepcopy(r.result);data['summary']='新的观察';r.result=data
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        assert db.get(Job,jid).state=='failed'
        assert db.scalar(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid)).reply is None
    assert services.counts['director_chat']==0


def test_new_observations_remove_stale_assistant_precedent_not_user_intent(rendered,transport):
    from types import SimpleNamespace
    c,repo,grant,vid,path=rendered;submit(rendered);run(repo)
    with repo.factory() as db:
        sequence=sr.context(repo,db,db.get(Version,vid))
        rows=[SimpleNamespace(question='保留完整原话',reply='错误旧推断',context={}),
              SimpleNamespace(question='再看看',reply='有新观察的回复',context={'sequence_check':sequence})]
        _,ctx=director_chat._context(repo,db,db.get(Episode,grant['episode_id']),rows,'怎么剪',vid,'preview')
        assert [m['text'] for m in ctx['history']]==['保留完整原话','再看看','有新观察的回复']
        assert ctx['history_omitted_count']==1 and rows[0].reply=='错误旧推断'


def test_report_changed_after_reply_marks_card_stale_and_cannot_apply(rendered,transport):
    c,repo,grant,vid,path=rendered;review=submit(rendered);run(repo);services=ChatServices()
    services.reply={'reply':'先去看当前版本。','action':{'kind':'navigate','title':'看当前版本',
                    'explanation':'先核对观察。','target_step':'preview'}}
    with repo.transaction() as db:
        job=director_chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,
            client_id='sequence-stale-card',text='去看预览',version_id=vid,step='preview',confirm_send=True);jid=job.id
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.transaction() as db:
        turn=db.scalar(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid));tid=turn.id
        report=db.get(Job,review['id']);new=deepcopy(report.result);new['summary']='新观察';report.result=new
    state=c.get(f'/api/v1/episodes/{grant["episode_id"]}/director/chat').json()
    assert state['proposals'][-1]['status']=='stale'
    result=c.post(f'/api/v1/episodes/{grant["episode_id"]}/director/actions/{tid}',json={'confirm':True})
    assert result.status_code==409 and result.json()['error']['code']=='SEQUENCE_REVIEW_STALE'


def propose_review(data, *, start=1., end=3., after_send=None):
    c,repo,grant,vid,path=data
    services=ChatServices();services.after_send=after_send
    services.reply={'reply':'先看看这段动作，再决定怎么剪。','action':{
        'kind':'review_sequence','title':'查看这段动作','explanation':'先看清过程，不直接剪。',
        'start':start,'end':end}}
    with repo.transaction() as db:
        job=director_chat.enqueue(repo,db,db.get(Episode,grant['episode_id']),services,
            client_id='review-card',text='请先看看这段动作',version_id=vid,step='preview',confirm_send=True)
        jid=job.id
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:
        job=db.get(Job,jid)
        turn=db.scalar(select(DirectorChatTurn).where(DirectorChatTurn.job_id==jid))
        return jid,turn.id,job.state


def test_review_card_requires_click_persists_progress_reuses_result_and_never_edits(rendered,transport):
    c,repo,g,vid,path=rendered;original=path.read_bytes()
    jid,tid,status=propose_review(rendered);assert status=='succeeded'
    url=f'/episodes/{g["episode_id"]}/director/actions/{tid}'
    state_url=f'/api/v1/episodes/{g["episode_id"]}/director/chat'
    card=c.get(state_url).json()['proposals'][-1]
    assert card['kind']=='review_sequence' and card['status']=='pending'
    assert (card['start'],card['end'])==(1.,3.)
    assert 'sequence_binding' not in card and not transport
    with repo.factory() as db:
        before=len(list(db.scalars(select(Version))))
        assert not list(db.scalars(select(Job).where(Job.kind=='sequence_review')))
    post(c,url,{'confirm':False},409)
    result=post(c,url,{'confirm':True});assert result['version_id']==vid and result['next_step'] is None
    assert post(c,url,{'confirm':True})==result
    card=c.get(state_url).json()['proposals'][-1]
    assert card['status']=='applied' and card['review_state']=='queued'
    run(repo);repo.recover()
    card=c.get(state_url).json()['proposals'][-1]
    assert card['review_state']=='succeeded' and card['review_summary']=='抽样可见物体位置变化。'
    assert post(c,url,{'confirm':True})==result and len(transport)==1
    with repo.factory() as db:
        assert len(list(db.scalars(select(Version))))==before
        v=db.get(Version,vid);assert v.state=='ready' and v.confirmed_at is None
    assert path.read_bytes()==original
    path.write_bytes(original+b'changed')
    assert c.get(state_url).json()['proposals'][-1]['review_state']=='stale'


@pytest.mark.parametrize('mutation',['output','grant','prompt'])
def test_review_card_stale_before_apply_cannot_queue(rendered,mutation,monkeypatch,tmp_path):
    c,repo,g,vid,path=rendered;_,tid,status=propose_review(rendered);assert status=='succeeded'
    if mutation=='output':path.write_bytes(path.read_bytes()+b'changed')
    elif mutation=='grant':
        from app.services.material_trial import GRANT_FILE
        (repo.data_dir/GRANT_FILE).unlink()
    else:
        prompt=tmp_path/'new.txt';prompt.write_text('new');monkeypatch.setattr(sr,'PROMPT',prompt)
    card=c.get(f'/api/v1/episodes/{g["episode_id"]}/director/chat').json()['proposals'][-1]
    assert card['status']=='stale'
    response=c.post(f'/api/v1/episodes/{g["episode_id"]}/director/actions/{tid}',json={'confirm':True})
    assert response.status_code==(400 if mutation=='grant' else 409)
    assert response.json()['error']['code']==('GRANT_INVALID' if mutation=='grant' else 'REVIEW_OUTPUT_CHANGED')
    with repo.factory() as db:assert not list(db.scalars(select(Job).where(Job.kind=='sequence_review')))


def test_review_changed_during_advice_cannot_publish_card(rendered):
    path=rendered[4]
    _,tid,status=propose_review(rendered,after_send=lambda:path.write_bytes(path.read_bytes()+b'changed'))
    assert status=='failed'
    with rendered[1].factory() as db:assert db.get(DirectorChatTurn,tid).proposal is None


@pytest.mark.parametrize('start,end',[(2.,1.),(0.,25.),(0.,9.)])
def test_model_invalid_window_cannot_publish_review_card(rendered,start,end):
    _,tid,status=propose_review(rendered,start=start,end=end)
    assert status=='failed'
    with rendered[1].factory() as db:assert db.get(DirectorChatTurn,tid).proposal is None


def test_review_card_failed_job_does_not_retry_and_owner_is_checked(rendered):
    c,repo,g,vid,path=rendered;_,tid,_=propose_review(rendered)
    url=f'/episodes/{g["episode_id"]}/director/actions/{tid}'
    result=post(c,url,{'confirm':True})
    with repo.transaction() as db:
        job=db.get(Job,result['job_id']);job.state='failed';job.error={'code':'TEST','message':'本次未完成，请先核对费用。'}
    repo.recover()
    card=c.get(f'/api/v1/episodes/{g["episode_id"]}/director/chat').json()['proposals'][-1]
    assert card['review_state']=='failed' and card['review_summary']=='本次未完成，请先核对费用。'
    assert post(c,url,{'confirm':True})==result
    auth=c.post('/api/v1/auth/login',json={'invite_code':'other'}).json();c.headers['X-CSRF-Token']=auth['csrf_token']
    assert c.post('/api/v1'+url,json={'confirm':True}).status_code==404


def store_observation(data,start,end,order,*,summary='合成观察',stale=False):
    """Explicit synthetic saved reports for aggregation and binding tests."""
    c,repo,g,vid,path=data;window={'start':float(start),'end':float(end)}
    with repo.transaction() as db:
        v=db.get(Version,vid);bound=sr.snapshot(repo,db,v,window)
        if stale:bound={**bound,'output_sha256':'stale'}
        job=Job(owner_id=g['owner_id'],episode_id=g['episode_id'],version_id=vid,
            kind='sequence_review',state='succeeded',idempotency_key='fixture-'+str(order),
            created_at=f'2026-11-17T00:00:{order:02d}',payload={'window':window,'snapshot':bound},
            result={'summary':summary,'observations':[{'start':start+.1,'end':end-.1,
                'visible_change':'合成物体移动','uncertainty':'仅作数据聚合测试'}]})
        db.add(job);db.flush();return job.id


def test_multiple_windows_survive_latest_report_and_gaps_are_explicit(rendered):
    c,repo,g,vid,path=rendered
    store_observation(rendered,4,7,1,summary='中段')
    store_observation(rendered,0,2,2,summary='开头')
    with repo.factory() as db:
        r=sr.context(repo,db,db.get(Version,vid))
        assert [w['summary'] for w in r['windows']]==['开头','中段']
        assert r['unobserved_windows']==[{'start':2.,'end':4.},{'start':7.,'end':8.}]
        assert not r['audio_analyzed'] and r['verification']=='unverified'
        assert not list(db.scalars(select(ModelCall)))


def test_stale_latest_does_not_hide_valid_old_report_and_same_window_deduplicates(rendered):
    c,repo,g,vid,path=rendered
    store_observation(rendered,1,3,1,summary='旧观察')
    store_observation(rendered,1,3,2,summary='新观察')
    store_observation(rendered,4,7,3,stale=True)
    with repo.factory() as db:
        r=sr.context(repo,db,db.get(Version,vid))
        assert len(r['windows'])==1 and r['windows'][0]['summary']=='新观察'


def test_observation_window_cap_keeps_recent_four_and_declares_omission(rendered):
    c,repo,g,vid,path=rendered
    for i in range(5):store_observation(rendered,i,i+1,i)
    with repo.factory() as db:
        r=sr.context(repo,db,db.get(Version,vid))
        assert [w['window']['start'] for w in r['windows']]==[1,2,3,4]
        assert r['reports_omitted']==1 and r['unobserved_windows'][0]=={'start':0.,'end':1.}


def test_changing_earlier_window_invalidates_pending_advice(rendered):
    c,repo,g,vid,path=rendered;earlier=store_observation(rendered,0,2,1)
    store_observation(rendered,4,7,2);services=ChatServices()
    with repo.transaction() as db:
        job=director_chat.enqueue(repo,db,db.get(Episode,g['episode_id']),services,
            client_id='aggregate-stale',text='这两段怎么接',version_id=vid,step='preview',confirm_send=True);jid=job.id
        old=db.get(Job,earlier);old.result={**old.result,'summary':'旧段结果已经变化'}
    Worker(repo,Pipeline(repo,services)).process_one()
    with repo.factory() as db:assert db.get(Job,jid).state=='failed'
    assert services.counts['director_chat']==0


def test_duplicate_review_advice_is_explicitly_rejected_without_another_card(rendered):
    c,repo,g,vid,path=rendered;original=path.read_bytes()
    stored=store_observation(rendered,1,3,1)
    jid,tid,status=propose_review(rendered)
    assert status=='succeeded'
    with repo.factory() as db:
        turn=db.get(DirectorChatTurn,tid)
        assert turn.proposal is None
        assert '没有通过检查' in turn.reply and '没有再次发起动作观察' in turn.reply
        assert db.get(Job,jid).result['suggestion_check']=='duplicate_review_rejected'
        assert [j.id for j in db.scalars(select(Job).where(Job.kind=='sequence_review'))]==[stored]
        assert db.get(Version,vid).confirmed_at is None
    assert path.read_bytes()==original
    repo.recover()
    state=c.get(f'/api/v1/episodes/{g["episode_id"]}/director/chat').json()
    assert not state['proposals']


@pytest.mark.parametrize('stale,start,end',[(True,1,3),(False,0,2)])
def test_obsolete_or_different_observation_does_not_block_new_review(rendered,stale,start,end):
    c,repo,g,vid,path=rendered
    store_observation(rendered,start,end,1,stale=stale)
    jid,tid,status=propose_review(rendered)
    assert status=='succeeded'
    with repo.factory() as db:
        assert db.get(DirectorChatTurn,tid).proposal['kind']=='review_sequence'
        assert db.get(Job,jid).result['suggestion_check']=='not_evaluated'
