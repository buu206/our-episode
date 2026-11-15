"""Review source timing and caption metadata; never infer what was heard or seen."""
from .dialogue_captions import review_flags


def inspect_source(episode, segments):
    confirmed = episode.evidence_confirmed_revision == episode.evidence_revision
    result = {'scope': 'source_metadata', 'evidence_confirmed': confirmed,
              'speech_checked': 0, 'captions_checked': 0, 'hidden_captions': 0,
              'issue_count': 0, 'issues': [],
              'not_checked': ['原声识别是否正确', '烧录字幕是否与实际声音同步', '故事与笑点是否成立']}
    if not confirmed:
        return result

    def add(code, index, start, end, message):
        result['issue_count'] += 1
        for issue in result['issues']:
            if (issue['code'], issue['segment_number'], issue['message']) == (code, index + 1, message):
                issue['count'] += 1
                return
        if len(result['issues']) < 8:
            result['issues'].append({'code': code, 'segment_number': index + 1,
                'source_start': start, 'source_end': end, 'message': message, 'count': 1})

    # Adjacent cuts of the same source keep speech continuous. Do not report
    # an artificial sentence cut just because a harmless shot boundary exists.
    runs = []
    for index, segment in enumerate(segments):
        if (runs and runs[-1]['asset_id'] == segment['asset_id']
                and abs(runs[-1]['end'] - segment['start']) <= .03
                and segment.get('transition', 'cut') == 'cut'):
            runs[-1]['end'] = segment['end']
        else:
            runs.append({'asset_id': segment['asset_id'], 'start': segment['start'],
                         'end': segment['end'], 'index': index})
    for run in runs:
        for line in episode.evidence:
            if line.get('kind') != 'speech' or line['asset_id'] != run['asset_id']:
                continue
            start, end = max(run['start'], line['start']), min(run['end'], line['end'])
            if end - start <= .03:
                continue
            result['speech_checked'] += 1
            if run['start'] > line['start'] + .03 or run['end'] < line['end'] - .03:
                add('SPEECH_EDGE_CUT', run['index'], start, end,
                    '这句原话可能没留完整，建议把前后半句一起带上。')
    for index, segment in enumerate(segments):
        for row in segment.get('_dialogue', []):
            if row.get('hidden'):
                result['hidden_captions'] += 1
                continue
            result['captions_checked'] += 1
            flags = review_flags(row)
            # A user-reviewed repeated word/whole-utterance alignment is not
            # repeatedly presented as an unresolved transcription error.
            if row.get('reviewed'):
                flags = [flag for flag in flags if flag in ('句子偏长', '显示偏快')]
            if flags:
                add('CAPTION_REVIEW', index, row['start'], row['end'], '；'.join(flags))
    return result
