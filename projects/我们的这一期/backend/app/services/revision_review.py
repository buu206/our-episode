"""Measure an edit against its baseline; never turn a delta into quality approval."""
from .editing import EditSegment


def _sequence(segments):
    # Adjacent pieces of the same original are still one uninterrupted take.
    result = []
    for row in segments:
        item = (row['asset_id'], row['start'], row['end'])
        if result and result[-1][0] == item[0] and abs(result[-1][2] - item[1]) < 1e-6:
            result[-1] = (item[0], result[-1][1], item[2])
        else:
            result.append(item)
    return result


def _coverage(sequence):
    result = {}
    for asset, start, end in sorted(sequence):
        spans = result.setdefault(asset, [])
        if spans and start <= spans[-1][1]:
            spans[-1][1] = max(end, spans[-1][1])
        else:
            spans.append([start, end])
    return result


def _subtract(left, right):
    result = []
    for asset, spans in left.items():
        for start, end in spans:
            cursor = start
            for a, b in right.get(asset, []):
                if b <= cursor or a >= end:
                    continue
                if a > cursor:
                    result.append({'asset_id': asset, 'start': cursor, 'end': min(a, end)})
                cursor = max(cursor, b)
            if cursor < end:
                result.append({'asset_id': asset, 'start': cursor, 'end': end})
    return result


def compare(before, after, evidence):
    """Inputs are validated edit segments; reports use original source seconds."""
    before = [EditSegment.model_validate(row).model_dump() for row in before]
    after = [EditSegment.model_validate(row).model_dump() for row in after]
    old, new = _sequence(before), _sequence(after)
    old_coverage, new_coverage = _coverage(old), _coverage(new)
    removed, added = _subtract(old_coverage, new_coverage), _subtract(new_coverage, old_coverage)
    speech = []
    for row in evidence:
        if row.get('kind') != 'speech' or not row.get('id'):
            continue
        def retained(coverage):
            return sum(max(0, min(b, row['end']) - max(a, row['start']))
                       for a, b in coverage.get(row['asset_id'], []))
        old_seconds, new_seconds = retained(old_coverage), retained(new_coverage)
        if old_seconds > new_seconds + 1e-6:
            speech.append({'evidence_id': row['id'], 'asset_id': row['asset_id'],
                           'start': row['start'], 'end': row['end'],
                           'impact': 'removed' if new_seconds < 1e-6 else 'partially_removed'})
    if removed or added:
        change = 'selection_changed'
    elif old != new:
        change = 'order_or_repeat_changed'
    else:
        change = 'same_source_sequence'
    return {'scope': 'mechanical_edit_difference', 'source_times': True,
            'change': change, 'removed': removed, 'added': added,
            'source_duration_before': round(sum(b-a for _, a, b in old), 6),
            'source_duration_after': round(sum(b-a for _, a, b in new), 6),
            'affected_speech': speech, 'quality': 'not_assessed',
            'not_checked': ['对白之外的声音和动作', '跳切是否自然、故事是否完整',
                            '转场、包装及旁白的视听效果']}


def directions(before, proposals, evidence):
    return [{'direction_id': item.id,
             **compare(before, [s.model_dump() for s in item.edit_plan.segments], evidence)}
            for item in proposals if item.edit_plan is not None]


def describe_cut(before, after, evidence):
    report = compare(before, after, evidence)
    removed = report['affected_speech']
    if any(row['impact'] != 'removed' for row in removed):
        speech = '有已确认对白被截短，需要重新检查。'
    elif removed:
        speech = f'整句舍弃{len(removed)}条已确认对白，其他已确认对白保留。'
    else:
        speech = '已确认对白全部保留。'
    duration = f"原片选段从{report['source_duration_before']:g}秒缩到{report['source_duration_after']:g}秒。"
    # Timing/retention claims come from the actual plan, not model prose.
    summary = duration + speech + '对应的画面和原声一起移除，衔接是否自然还需预览。'
    reply = '我先给你一版试剪建议。\n\n' + duration + '\n' + speech + '\n\n对应画面和原声会一起缩短，先看衔接再决定，原版保留。'
    return summary, reply, report
