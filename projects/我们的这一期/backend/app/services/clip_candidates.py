"""Opt-in planning prototype: choose immutable clips, never model-written times.

Not enabled in the live director. Candidates preserve timed speech, but neither
their existence nor successful compilation establishes story continuity.
"""
import hashlib
import json

from .director import InputSpan, DirectorError, _assets_valid, _check_span
from .editing import validate_director_segments, timeline
from .source_windows import planning_sources


def build_clip_candidates(evidence, assets):
    _assets_valid(assets)
    rows = [InputSpan.model_validate(row).model_dump(exclude_none=True) for row in evidence]
    if not rows or len(rows) > 100 or any(not r.get('id') or r['kind']=='continuity' for r in rows):
        raise DirectorError('INVALID_EVIDENCE', '候选片段需要带唯一标识的原始观察。')
    if len({r['id'] for r in rows}) != len(rows):
        raise DirectorError('INVALID_EVIDENCE', '来源标识重复。')
    for row in rows:
        _check_span(InputSpan.model_validate(row), assets)
    sources = planning_sources(rows)
    speech = sorted((r for r in rows if r['kind']=='speech'), key=lambda r:(r['asset_id'],r['start'],r['end'],r['id']))
    fingerprint = hashlib.sha256(json.dumps([rows, assets], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    seeds = [(r['asset_id'], r['start'], r['end']) for r in speech]
    for left, right in zip(speech, speech[1:]):
        if left['asset_id']==right['asset_id'] and 0 <= right['start']-left['end'] <= 1:
            seeds.append((left['asset_id'],left['start'],right['end']))
    clips, seen = [], set()
    for aid, start, end in seeds:
        same = [r for r in speech if r['asset_id']==aid]
        # Include concurrent/overlapping utterances whole; never shave them off.
        while True:
            overlap = [r for r in same if min(end,r['end']) > max(start,r['start'])]
            expanded = (min([start]+[r['start'] for r in overlap]), max([end]+[r['end'] for r in overlap]))
            if expanded == (start,end):
                break
            start,end = expanded
        covers = [r for r in sources if r['asset_id']==aid and r['start']<=start and r['end']>=end]
        if not covers or end-start>12:
            continue
        container = max(covers, key=lambda r:r['end']-r['start'])
        # Modest original context; no overlap with an unselected utterance.
        start = max(container['start'],start-.35,
                    max((r['end'] for r in same if r['end']<=start), default=0.))
        end = min(container['end'],end+.5,
                  min((r['start'] for r in same if r['start']>=end), default=assets[aid]))
        if end-start < .1 or end-start > 12 or (aid,start,end) in seen:
            continue
        seen.add((aid,start,end))
        refs = [container['id']] + [r['id'] for r in rows if r['asset_id']==aid and min(end,r['end'])>max(start,r['start'])]
        refs = list(dict.fromkeys(refs))
        if len(refs)>40:
            continue
        clip_id = 'clip_' + hashlib.sha256(f'{fingerprint}:{aid}:{start:.9f}:{end:.9f}'.encode()).hexdigest()[:20]
        shot = {'asset_id':aid,'start':start,'end':end,'caption':'', 'purpose':'完整原声候选，故事用途待编导选择',
                'evidence_ids':refs,'transition':'cut','transition_duration':0}
        validate_director_segments([shot], rows)
        clips.append({'clip_id':clip_id, **shot, 'speech_ids':[r['id'] for r in same
            if min(end,r['end'])>max(start,r['start'])],
            'status':'unreviewed_candidate', 'story_continuity_verified':False})
    return sorted(clips,key=lambda c:(c['asset_id'],c['start'],c['end']))


def compile_clip_selection(selection, evidence, assets, *, duration_range=None):
    """Rebuild from trusted source data; only IDs and editorial purpose come in."""
    catalog = {c['clip_id']:c for c in build_clip_candidates(evidence,assets)}
    if not isinstance(selection,list) or not 1<=len(selection)<=30:
        raise DirectorError('INVALID_CLIP_SELECTION','需选择1—30个候选片段。')
    segments, used = [], set()
    for choice in selection:
        if (not isinstance(choice,dict) or set(choice)!={'clip_id','purpose'}
                or not isinstance(choice['clip_id'],str) or choice['clip_id'] not in catalog
                or not isinstance(choice['purpose'],str) or not choice['purpose'].strip()
                or len(choice['purpose'])>200 or choice['clip_id'] in used):
            raise DirectorError('INVALID_CLIP_SELECTION','片段标识或用途无效；不能自填时间或引用。')
        c = catalog[choice['clip_id']]
        if any(s['asset_id']==c['asset_id'] and min(s['end'],c['end'])>max(s['start'],c['start']) for s in segments):
            raise DirectorError('OVERLAPPING_CLIPS','所选片段重复覆盖原片，需重新选择。')
        used.add(choice['clip_id'])
        segments.append({k:c[k] for k in ('asset_id','start','end','caption','evidence_ids','transition','transition_duration')})
        segments[-1]['purpose']=choice['purpose']
    validate_director_segments(segments,evidence)
    if duration_range is not None:
        low,high=duration_range
        if not 0<low<=high<=90:
            raise DirectorError('INVALID_DURATION','目标时长范围无效。')
        if not low<=timeline(segments)[-1].end<=high:
            raise DirectorError('BRIEF_DURATION_MISMATCH','所选片段未满足目标时长。')
    return segments
