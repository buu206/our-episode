"""Scoped production HTTP/jobs, synthetic files and mocked model HTTP; never real AI claims."""
import json
from copy import deepcopy
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from app.config import Settings
from app.main import create_app
from app.models import Episode, Job, ModelCall, Version
from app.services.pipeline import ModelServices, Pipeline
from app.services import workflow_access as access
from app.worker import Worker
from test_authorized_materials import setup, movie, confirmed, director_transport, transport, offline, KEYS
from test_authorized_narration import transport as voice_transport, TEXT

B='/api/v1'

def grant_file(repo,grant):
    path=repo.data_dir/access.GRANT_FILE
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(grant))
    return path

@pytest.fixture
def client(confirmed):
    repo,grant=confirmed
    grant_file(repo,grant)
    with TestClient(create_app(Settings(data_dir=repo.data_dir, invites={'test':'local-owner','other':'other'},
            worker_enabled=False,cloud=KEYS))) as c:
        r=c.post(B+'/auth/login',json={'invite_code':'test'})
        c.headers['X-CSRF-Token']=r.json()['csrf_token']
        yield c,repo,grant

def post(c,path,body=None,status=200):
    r=c.post(B+path,json=body or {})
    assert r.status_code==status,r.text
    return r.json()

def advance(c,repo,jid,state='succeeded'):
    Worker(repo,Pipeline(repo,ModelServices(KEYS))).process_one()
    r=c.get(B+'/jobs/'+jid).json()
    assert r['state']==state,r
    return r

def test_cached_analysis_is_visible_and_preserves_versions(client):
    c,repo,g=client
    with repo.transaction() as db:
        db.add(Version(id='a'*32,owner_id=g['owner_id'],episode_id=g['episode_id'],title='old',state='confirmed',segments=[]))
    state=c.get(B+'/episodes/'+g['episode_id']).json()
    assert state['workflow']['enabled'] and state['workflow']['analysis_cached']
    assert c.get(B+'/status').json()['cloud_enabled'] is False
    j=post(c,f"/episodes/{g['episode_id']}/analysis")
    assert j['state']=='succeeded'
    with repo.factory() as db:
        assert db.get(Episode,g['episode_id']).evidence_revision==5
        assert db.get(Version,'a'*32).state=='confirmed'
        assert not list(db.scalars(select(ModelCall)))


def test_production_direction_to_render_confirm_export(client,director_transport):
    c,repo,g=client
    job=post(c,f"/episodes/{g['episode_id']}/directions")
    assert post(c,f"/episodes/{g['episode_id']}/directions")['id']==job['id']
    advance(c,repo,job['id'])
    assert len(director_transport['calls'])==1
    v=post(c,f"/episodes/{g['episode_id']}/direction",{'direction_id':'d1','revision':5,'brief_revision':0,'packaging':'standard'},201)
    j=post(c,f"/versions/{v['id']}/render")
    advance(c,repo,j['id'])
    assert c.get(B+f"/versions/{v['id']}/export").status_code==409
    post(c,f"/versions/{v['id']}/confirm")
    download=c.get(B+f"/versions/{v['id']}/export")
    assert download.status_code==200 and len(download.content)>1000
    assert c.get(B+f"/versions/{v['id']}/export").content==download.content


def test_normal_page_narration_requires_confirm_and_reuses_audio(client,voice_transport):
    c,repo,g=client
    with repo.transaction() as db:
        v=Version(id='a'*32,owner_id=g['owner_id'],episode_id=g['episode_id'],title='test',origin='ai',
            evidence_revision=5,narration_text=TEXT,segments=[{'asset_id':g['assets'][0]['id'],'start':1.,'end':3.,'caption':'测试'}])
        db.add(v)
    post(c,'/versions/'+v.id+'/narration',{'confirm_text':False},409)
    assert not voice_transport['calls']
    j=post(c,'/versions/'+v.id+'/narration',{'confirm_text':True})
    advance(c,repo,j['id'])
    assert len(voice_transport['calls'])==1
    assert post(c,'/versions/'+v.id+'/narration',{'confirm_text':True})['id']==j['id']
    with repo.factory() as db:
        assert db.get(Version,v.id).state=='draft'
        assert db.get(Version,v.id).confirmed_at is None


@pytest.mark.parametrize('change',['remove','hash','owner','grant'])
def test_queue_rechecks_grant_before_any_send(client,director_transport,change):
    c,repo,g=client
    job=post(c,f"/episodes/{g['episode_id']}/directions")
    if change=='remove': (repo.data_dir/access.GRANT_FILE).unlink()
    elif change=='hash': (repo.data_dir/g['assets'][0]['path']).write_bytes(b'changed')
    elif change=='owner':
        with repo.transaction() as db: db.get(Episode,g['episode_id']).owner_id='other'
    else:
        g['assets'][0]['sha256']='f'*64
        grant_file(repo,g)
    Worker(repo,Pipeline(repo,ModelServices(KEYS))).process_one()
    with repo.factory() as db:
        assert db.get(Job,job['id']).state=='failed'
        assert not list(db.scalars(select(ModelCall)))
    assert not director_transport['calls']


def test_other_episode_not_enabled_and_other_user_cannot_read(client):
    c,repo,g=client
    other=post(c,'/episodes',{'title':'ungranted','background':''},201)
    assert other['workflow'] is None
    post(c,f"/episodes/{other['id']}/analysis",status=409)
    r=c.post(B+'/auth/login',json={'invite_code':'other'})
    assert c.get(B+'/episodes/'+g['episode_id']).status_code==404


def test_scoped_analysis_enters_from_api(setup,transport):
    repo,g=setup;grant_file(repo,g)
    with TestClient(create_app(Settings(data_dir=repo.data_dir,invites={'test':'local-owner'},worker_enabled=False,cloud=KEYS))) as c:
        r=c.post(B+'/auth/login',json={'invite_code':'test'});c.headers['X-CSRF-Token']=r.json()['csrf_token']
        j=post(c,f"/episodes/{g['episode_id']}/analysis")
        advance(c,repo,j['id'])
        assert len(transport['calls'])==4
        assert post(c,f"/episodes/{g['episode_id']}/analysis")['id']==j['id']
        state=c.get(B+'/episodes/'+g['episode_id']).json()
        assert state['analysis_state']=='review' and state['evidence_confirmed_revision'] is None
        assert state['workflow']['analysis_cached']
