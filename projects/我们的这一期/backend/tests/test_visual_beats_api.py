from sqlalchemy import select
import pytest

from app.models import Version
from app.worker import Worker
from test_api import settings, client, headers, episode, asset, video_bytes, BASE


def payload(asset_id):
    return {'title':'视觉强调试验','packaging':'variety_v2','segments':[{
        'asset_id':asset_id,'start':0,'end':.5,'caption':'','visual_beats':[{
            'kind':'focus','source_time':.05,'duration':.4,'text':'细节',
            'target':{'x':.1,'y':.1,'width':.3,'height':.3},'position':'top_left',
        }],
    }]}


@pytest.mark.parametrize('kind', ['focus', 'cutout', 'headline'])
def test_api_stores_renders_and_reads_effects_without_confirming(client,headers,episode,asset,kind):
    body=payload(asset['id'])
    beat=body['segments'][0]['visual_beats'][0]
    beat['kind']=kind
    if kind=='cutout':
        beat['contour']=[{'x':.1,'y':.1},{'x':.4,'y':.3},{'x':.1,'y':.4}]
    r=client.post(BASE+f"/episodes/{episode['id']}/versions",headers=headers,json=body)
    assert r.status_code==201,r.text
    version=r.json();vid=version['id']
    assert version['segments'][0]['visual_beats']==body['segments'][0]['visual_beats']
    assert version['origin']=='manual'
    job=client.post(BASE+f'/versions/{vid}/render',headers=headers).json()
    Worker(client.app.state.repo).process_one()
    result=client.get(BASE+f"/jobs/{job['id']}",headers=headers).json()
    assert result['state']=='succeeded',result
    assert result['result']['visual_beats_count']==1
    with client.app.state.repo.factory() as db:
        saved=db.scalar(select(Version).where(Version.id==vid))
        assert saved.state=='ready' and saved.confirmed_at is None
        assert saved.segments[0]['visual_beats']==body['segments'][0]['visual_beats']
    assert client.get(BASE+f'/versions/{vid}/export',headers=headers).status_code==409


def test_api_rejects_effect_outside_clip_and_wrong_packaging(client,headers,episode,asset):
    body=payload(asset['id']);body['segments'][0]['visual_beats'][0]['source_time']=.9
    assert client.post(BASE+f"/episodes/{episode['id']}/versions",headers=headers,json=body).status_code==422
    body=payload(asset['id']);body['packaging']='original'
    assert client.post(BASE+f"/episodes/{episode['id']}/versions",headers=headers,json=body).status_code==422
