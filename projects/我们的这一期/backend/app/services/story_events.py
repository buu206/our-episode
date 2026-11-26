"""Source-bound editorial units. Labels/decisions are proposals, never facts."""
from copy import deepcopy
from ..repository import DomainError
from .editing import timeline
from .director_cut import requested_speech_ids


def build(context):
    # New conversations freeze numbering with the exact request. A paid answer
    # resumed later must not be reinterpreted using a newer partition recipe.
    if 'story_event_catalog' in context:
        return deepcopy(context['story_event_catalog'])
    return derive(context)


def derive(context):
    current = context.get('current_version') or {}
    segments = current.get('segments') or []
    if 'revise_cut' not in context.get('available_actions', []) or not segments:
        return []
    evidence = context.get('confirmed_evidence', [])
    protected = requested_speech_ids(context)
    windows = (context.get('sequence_check') or {}).get('windows', [])
    result = []
    for shot, (segment, timing) in enumerate(zip(segments, timeline(segments)), 1):
        speech = [r for r in evidence if r.get('kind') == 'speech'
                  and r['asset_id'] == segment['asset_id']
                  and r['end'] > segment['start'] and r['start'] < segment['end']]
        points = {segment['start'], segment['end']}
        for row in speech:
            points.update((max(segment['start'], row['start']), min(segment['end'], row['end'])))
        # Overlapping speakers form a single indivisible unit; no word cut.
        points = sorted(p for p in points if p in (segment['start'], segment['end'])
                        or not any(r['start'] < p < r['end'] for r in speech))
        # A long nonverbal interval can contain both the action and a pause.
        # Split only at recorded changes from dense sampling, never at invented
        # evenly spaced times; these remain approximate candidate boundaries.
        extras = []
        for left, right in zip(points, points[1:]):
            if right-left < 4 or any(r['start'] < right and r['end'] > left for r in speech):
                continue
            candidates = []
            for window in windows:
                sampling = window.get('sampling') or {}
                if sampling.get('continuous_video') is not False or sampling.get('max_sample_gap_seconds', 99) > .5:
                    continue
                for observation in window.get('observations', []):
                    for key in ('start', 'end'):
                        point = round(observation[key]-timing.start+segment['start'], 3)
                        if left+.5 <= point <= right-.5:
                            candidates.append(point)
            previous = left
            for point in sorted(set(candidates)):
                if point-previous >= .5:
                    extras.append(point)
                    previous = point
        points = sorted(set(points+extras))
        for start, end in zip(points, points[1:]):
            if end - start < .001:
                continue
            output_start = timing.start + start - segment['start']
            output_end = timing.start + end - segment['start']
            lines = [r['id'] for r in speech if r['start'] < end and r['end'] > start]
            observations = []
            for wi, window in enumerate(windows, 1):
                for oi, obs in enumerate(window.get('observations', []), 1):
                    if min(output_end, obs['end']) > max(output_start, obs['start']):
                        observations.append(f'w{wi}.o{oi}')
            result.append({'event': f'e{len(result)+1}', 'shot': shot,
                           'start': start, 'end': end,
                           'speech_evidence_ids': lines,
                           'observations': observations,
                           'must_keep': bool(protected.intersection(lines)),
                           'kind': 'speech' if lines else 'untranscribed_action'})
    # Never silently omit later events; use legacy selection if not representable.
    return result if len(result) <= 32 else []


def compile_decisions(context, decisions):
    events = build(context)
    expected = [e['event'] for e in events]
    if not events or [d['event'] for d in decisions] != expected:
        raise DomainError('INVALID_EVENT_PLAN', '故事段落已变化或没有逐段交代，未生成剪法。', 422)
    selections, review = [], []
    for event, decision in zip(events, decisions):
        if not decision['keep']:
            if event['must_keep']:
                raise DomainError('REQUESTED_SPEECH_REMOVED', '指定保留的对白不能舍弃，原版不变。', 409)
            if event['kind'] != 'speech' and not event['observations']:
                raise DomainError('UNOBSERVED_EVENT_REMOVED', '这段动作没有观察依据，不能仅因缺少转写就删除。', 422)
            if not decision.get('risk', '').strip():
                raise DomainError('INVALID_EVENT_PLAN', '舍弃故事段落须说明语境损失。', 422)
        else:
            item = {k: event[k] for k in ('shot', 'start', 'end')}
            item['reason'] = decision['reason']
            if (selections and selections[-1]['shot'] == item['shot']
                    and abs(selections[-1]['end'] - item['start']) < 1e-6):
                selections[-1]['end'] = item['end']
            else:
                selections.append(item)
        review.append({**event, **decision, 'quality': 'not_accepted'})
    if not selections or len(selections) > 8:
        raise DomainError('INVALID_EVENT_PLAN', '请保留故事且把剪法控制在8段以内。', 422)
    return selections, review


def requested(context):
    """Route explicit cut-card requests only; conversation stays conversational."""
    import re
    if not context:
        return False
    question = context.get('question', '')
    if re.search(r'不要(?:建议|修改|剪辑|镜头)?卡|不(?:要|用)试剪|只(?:解释|聊天|聊聊)', question):
        return False
    return bool(re.search(r'试剪|镜头调整卡|给.{0,12}剪法', question) and build(context))


def packet(context):
    rows = build(context)
    ids = {ref for row in rows for ref in row['speech_evidence_ids']}
    return {'question': context['question'], 'episode': context.get('episode'),
            'story_events': rows,
            'confirmed_evidence': [e for e in context.get('confirmed_evidence', []) if e.get('id') in ids],
            'sequence_check': context.get('sequence_check'),
            'editorial_timing': context.get('editorial_timing'),
            'protected_speech_evidence_ids': sorted(requested_speech_ids(context)),
            'available_actions': ['revise_cut'],
            'scope': 'proposal_only_not_execution',
            'not_checked': ['连续动作与同期声音', '剪点衔接效果', '最终故事质量']}
