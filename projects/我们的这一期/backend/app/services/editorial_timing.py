"""Put planned flower-text cues on the same clock as sampled observations.

Reuse the renderer's compiler; never infer what was visible or which action
finished from a timing intersection. Narrated-intro offsets are not guessed.
"""
from .editorial_plan import compile_editorial


def context(version, sequence=None):
    if (version is None or version.state not in ('ready', 'confirmed')
            or version.packaging != 'variety_v2' or version.narration_path):
        return None
    plan = compile_editorial(version.segments)
    cues = []
    for item in plan:
        beat = item['beat']
        if beat.treatment == 'none':
            continue
        overlaps = []
        for wi, window in enumerate((sequence or {}).get('windows', []), 1):
            for oi, observation in enumerate(window.get('observations', []), 1):
                start = max(item['start'], observation['start'])
                end = min(item['end'], observation['end'])
                if end - start > 1e-6:
                    overlaps.append({'observation': f'w{wi}.o{oi}',
                        'output_start': round(start, 3), 'output_end': round(end, 3)})
        cues.append({'shot': item['segment'] + 1, 'text': beat.text,
            'treatment': beat.treatment, 'output_start': round(item['start'], 3),
            'output_end': round(item['end'], 3), 'observation_overlaps': overlaps})
    if not cues:
        return None
    return {'scope': 'planned_editorial_timing', 'clock': 'output_seconds', 'cues': cues,
        'not_checked': ['布局可能跳过花字，计划时段不证明实际可见',
                        '时间重合不证明动作完成、人物意图或因果', '音效时点与连续动作质量']}
