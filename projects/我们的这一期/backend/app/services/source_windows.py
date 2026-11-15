"""Deterministic source-time containers, never new observed facts.

A bounded run in one original file can retain silence/actions between known
utterances. Its ID changes with member evidence; it cannot authorize a caption,
reaction, identity, or spatial effect in an unobserved gap.
"""
import hashlib
import json


def source_windows(evidence):
    groups = {}
    for row in evidence:
        if row.get('kind') == 'speech' and row.get('id'):
            groups.setdefault(row['asset_id'], []).append(row)
    result = []
    for asset, rows in sorted(groups.items()):
        runs = []
        for row in sorted(rows, key=lambda x: (x['start'], x['end'], x['id'])):
            if (not runs or row['start'] - max(x['end'] for x in runs[-1]) > 10
                    or max(row['end'], max(x['end'] for x in runs[-1])) - runs[-1][0]['start'] > 60):
                runs.append([])
            runs[-1].append(row)
        for run in runs:
            start, end = run[0]['start'], max(x['end'] for x in run)
            if len(run) < 2 or end - start < 8 or end - start > 60:
                continue
            identity = [[x['id'], x['start'], x['end'], x['note']] for x in run]
            key = hashlib.sha256(json.dumps([asset, identity], ensure_ascii=False).encode()).hexdigest()[:32]
            result.append({'id': 'run_' + key, 'kind': 'continuity', 'asset_id': asset,
                'start': start, 'end': end,
                'note': '同一原片按原顺序连续保留的时间容器；包含话轮之间未标注的画面和声音，不是事件描述，不证明动作、结果或人物反应。'})
    return result


def planning_sources(evidence):
    return list(evidence) + source_windows(evidence)
