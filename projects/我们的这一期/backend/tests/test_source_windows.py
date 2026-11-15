"""Continuous source playback is distinct from factual/effect evidence."""
from copy import deepcopy
import json
import httpx
import pytest
from app.services.source_windows import source_windows
from app.services.director import prepare_input, parse_result, check_grounding, DirectorError
from app.services.editing import validate_director_segments
from app.models import Episode
from test_workflow_access import client, post, advance
from test_authorized_materials import setup, confirmed, movie, offline
from test_new_providers import envelope, NOW


def evidence(asset='a'):
    return [dict(id='s1',asset_id=asset,kind='speech',start=0.,end=3.,note='先介绍一下'),
            dict(id='s2',asset_id=asset,kind='speech',start=7.,end=9.,note='完成了')]


def shot(rows):
    run=source_windows(rows)[0]
    return dict(asset_id=run['asset_id'],start=run['start'],end=run['end'],caption='',
                purpose='按原顺序保留话轮之间的原片过程',evidence_ids=[run['id']],
                editorial=dict(role='action',treatment='none',reason='未标注过程保持原声原画',evidence_ids=[run['id']]))


def proposal(rows):
    run=source_windows(rows)[0]
    return {'directions':[{'id':'d1','title':'原片过程','summary':'按原顺序保留过程，画面待预览核对',
        'evidence':[{k:run[k] for k in ('asset_id','start','end','note')}],
        'edit_plan':{'segments':[shot(rows)],'narration_text':''}}],'uncertainties':['中间画面尚未连续理解']}


def test_windows_are_separate_from_facts_and_reproducible():
    rows=evidence();before=deepcopy(rows)
    payload=json.loads(prepare_input(rows,{'a':12.}))
    assert payload['verified_evidence']==rows==before
    assert payload['source_windows']==source_windows(rows)
    assert source_windows(list(reversed(rows)))==source_windows(rows)
    valid=parse_result(json.dumps(proposal(rows),ensure_ascii=False),{'a':12.},require_edit_plan=True)
    check_grounding(valid,rows)
    validate_director_segments([shot(rows)],rows)
    changed=deepcopy(rows);changed[0]['note']='修订后'
    with pytest.raises(ValueError):validate_director_segments([shot(rows)],changed)


def test_cannot_submit_a_window_as_verified_evidence():
    rows=evidence()
    with pytest.raises(DirectorError):prepare_input(rows+source_windows(rows),{'a':12.})


@pytest.mark.parametrize('kind',['cut_word','outside','caption','effect_in_gap','foreign_source'])
def test_continuity_does_not_allow_new_facts_or_truncated_words(kind):
    rows=evidence();s=shot(rows)
    if kind=='cut_word':s['start']=1.
    if kind=='outside':s['end']=10.
    if kind=='caption':s.pop('editorial');s['caption']='他笑了'
    if kind=='foreign_source':s['asset_id']='b'
    if kind=='effect_in_gap':s['editorial'].update(treatment='accent',source_time=4.,duration=1.,text='哈哈哈')
    with pytest.raises(ValueError):validate_director_segments([s],rows)


def test_effect_still_needs_exact_fact_interval():
    rows=evidence();s=shot(rows);s['evidence_ids'].append('s2')
    s['editorial'].update(role='closing',treatment='accent',evidence_ids=['s2'],source_time=7.,duration=1.,text='完成了')
    validate_director_segments([s],rows)
    s['editorial']['source_time']=5.
    with pytest.raises(ValueError):validate_director_segments([s],rows)


def test_no_cross_file_or_unbounded_join():
    rows=evidence();rows[1]['asset_id']='b';assert source_windows(rows)==[]
    rows=evidence();rows[1].update(start=20.,end=22.);assert source_windows(rows)==[]
    rows=evidence();rows[0]['end']=61.;assert source_windows(rows)==[]
    assert source_windows([dict(asset_id='a',start=0.,end=9.,note='legacy')])==[]


def test_normal_api_select_and_render_preserves_continuous_run(client,monkeypatch):
    c,repo,g=client;aid=g['assets'][0]['id'];rows=evidence(aid);rows[1].update(start=6.,end=8.)
    with repo.transaction() as db:db.get(Episode,g['episode_id']).evidence=rows
    calls=[];original=httpx.AsyncClient
    def handler(request):
        body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'])
        assert payload['source_windows']==source_windows(rows)
        calls.append(1)
        return httpx.Response(200,json=envelope(proposal(rows)))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    job=post(c,f"/episodes/{g['episode_id']}/directions");advance(c,repo,job['id'])
    v=post(c,f"/episodes/{g['episode_id']}/direction",{'direction_id':'d1','revision':5,'brief_revision':0,'packaging':'variety_v2'},201)
    assert v['segments'][0]['end']-v['segments'][0]['start']==8.
    j=post(c,f"/versions/{v['id']}/render");done=advance(c,repo,j['id'])
    assert done['result']['duration']>=7.9 and done['result']['has_audio']
    assert len(calls)==1
