"""Map confirmed source speech onto an eligible output; never infer from silence."""
from .editing import timeline


def context(episode, version):
    # Other styles may insert a separate intro. Do not guess those offsets.
    if (not version or version.state not in ('ready', 'confirmed')
            or version.packaging != 'variety_v2' or version.narration_path
            or episode.evidence_confirmed_revision != episode.evidence_revision
            or episode.analysis_state != 'confirmed'):
        return None
    timings = timeline(version.segments)
    speech = [e for e in episode.evidence if e.get('kind') == 'speech']
    rows = []
    for index, (segment, timing) in enumerate(zip(version.segments, timings)):
        for line in speech:
            if line['asset_id'] != segment['asset_id']:
                continue
            start, end = max(line['start'], segment['start']), min(line['end'], segment['end'])
            if end <= start:
                continue
            rows.append({'evidence_id': line['id'], 'shot': index+1,
                'output_start': round(timing.start + start - segment['start'], 3),
                'output_end': round(timing.start + end - segment['start'], 3),
                'whole_utterance': line['start'] >= segment['start']-.03 and line['end'] <= segment['end']+.03})
    rows.sort(key=lambda r: (r['output_start'], r['shot'], r['evidence_id']))
    return {'scope': 'confirmed_speech_mapping', 'speech': rows[:64], 'omitted': max(0, len(rows)-64),
            'not_checked': ['没有条目不等于无声或没有动作', '说话人身份、情绪、画面动作与对白的因果']}
