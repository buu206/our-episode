"""Captions from confirmed speech; reuse ASR timing only when text still matches."""
import math
import hashlib
import re
from sqlalchemy import select
from ..models import Job
from .editing import timeline


def compact(text):
    return re.sub(r'\s+', '', text)


def display_text(text):
    # Preserve English word boundaries; remove ASR's spaces between Chinese glyphs.
    text=re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])','',text.strip())
    text=re.sub(r'\s+([，。！？；：、,.!?;:])',r'\1',text)
    return text.rstrip('，,。 ')


def attach_dialogue(db, episode, segments):
    if episode.evidence_confirmed_revision != episode.evidence_revision:
        attach_subtitle_draft(db, episode, segments)
        return
    jobs = db.scalars(select(Job).where(Job.episode_id == episode.id,
        Job.owner_id == episode.owner_id, Job.kind == 'analysis', Job.state == 'succeeded')
        .order_by(Job.created_at.desc())).all()
    # Validation results are not approved caption timing. Even identical words
    # can carry new timestamps and must not invalidate saved subtitle cue IDs.
    jobs = [job for job in jobs if not job.payload.get('validation_only')]
    for segment in segments:
        lines = []
        for evidence in episode.evidence:
            if evidence.get('kind') != 'speech' or evidence['asset_id'] != segment['asset_id']:
                continue
            if evidence['start'] < segment['start'] - .03 or evidence['end'] > segment['end'] + .03:
                continue
            words, aligned = [], False
            for job in jobs:
                candidate = [w for w in job.payload.get('steps', {}).get('asr:' + segment['asset_id'], [])
                    if w['start'] >= evidence['start'] - .02 and w['end'] <= evidence['end'] + .02]
                if compact(''.join(w['text'] for w in candidate)) == compact(evidence['note']):
                    words = candidate
                    aligned = True
                    break
            # An edited transcript never inherits guessed word alignment.
            if not words:
                words = [{'start': evidence['start'], 'end': evidence['end'], 'text': evidence['note']}]
            group = None
            for word in words:
                text = word['text'].strip()
                if (group and word['start'] - group['end'] < .3 and
                        not re.search(r'[，。！？；,.!?;]$',group['text']) and
                        len(compact(group['text'])) + len(compact(text)) <= 14 and word['end'] - group['start'] <= 3.5):
                    joiner=' ' if group['text'][-1:].isascii() and text[:1].isascii() and group['text'][-1:].isalnum() and text[:1].isalnum() else ''
                    group['text'] += joiner+text
                    group['end'] = word['end']
                else:
                    group = {'start': word['start'], 'end': word['end'], 'text': text,
                             'evidence_id': evidence['id'], 'context': compact(evidence['note']),
                             'alignment': 'asr' if aligned else 'utterance'}
                    lines.append(group)
        for row in lines:
            row['text']=display_text(row['text'])
            row['cue_id']=hashlib.sha256(f"{segment['asset_id']}:{row['evidence_id']}:{row['start']:.6f}:{row['end']:.6f}:{row['text']}".encode()).hexdigest()[:32]
        segment['_dialogue'] = [row for row in lines if row['text']]


def draft_rows_from_steps(steps, asset_ids):
    """Deterministic grouping of stored ASR, without inventing words or timings.

    Speaker labels are request-local: keep both label and scope at joins. Long
    provider utterances keep their timing and are flagged for review, never
    split into guessed word times. Uncertain speech stays editable, not deleted.
    """
    rows = []
    for aid in asset_ids:
        words = steps.get('asr:' + aid, [])
        if not isinstance(words, list):
            continue
        group = None
        for item in words:
            if not isinstance(item, dict) or item.get('asset_id') != aid:
                group = None
                continue
            a,b,text = item.get('start'),item.get('end'),item.get('text')
            if (type(a) not in (int,float) or type(b) not in (int,float)
                    or not math.isfinite(a) or not math.isfinite(b) or not 0 <= a < b
                    or not isinstance(text,str) or not text.strip() or len(text)>500
                    or any(ord(c)<32 for c in text)):
                group = None
                continue
            text = text.strip()
            speaker = (item.get('speaker_scope'), item.get('speaker'))
            # No overlap joins: concurrent speech needs separate captions.
            if (group and group['_speaker']==speaker and 0 <= a-group['end'] < .3
                    and not re.search(r'[，。！？；,.!?;]$', group['text'])
                    and len(compact(group['text']))+len(compact(text))<=14
                    and b-group['start']<=3.5):
                joiner=' ' if group['text'][-1:].isascii() and text[:1].isascii() and group['text'][-1:].isalnum() and text[:1].isalnum() else ''
                group['text']+=joiner+text
                group['end']=b
            else:
                group=dict(asset_id=aid,start=a,end=b,text=text,_speaker=speaker)
                rows.append(group)
    for row in rows:
        row.pop('_speaker')
    return rows


def published_revision(source):
    if source.kind == 'analysis':
        if source.payload.get('validation_only'):
            return None
        return (source.result or {}).get('revision')
    return source.payload.get('revision')


def attach_subtitle_draft(db, episode, segments):
    """A saved transcription draft can be edited without approving the evidence.

    The first draft for a source revision remains the caption baseline; later
    corrections use versioned subtitle edits, never a newer transcription job.
    This does not approve evidence or mark any caption reviewed.
    """
    drafts = db.scalars(select(Job).where(Job.episode_id == episode.id,
        Job.owner_id == episode.owner_id, Job.kind.in_(('subtitle_draft','analysis')),
        Job.state == 'succeeded').order_by(Job.created_at.asc(), Job.id.asc())).all()
    draft = next((j for j in drafts if published_revision(j) == episode.evidence_revision), None)
    if draft is None:
        return
    if draft.kind == 'subtitle_draft':
        source = db.get(Job, draft.payload.get('source_job_id'))
        rows = (draft.result or {}).get('rows', [])
    else:
        # A normal completed analysis already persists all ASR checkpoints.
        # Its published revision is newer than its input revision; use the
        # output revision to avoid attaching stale or validation-only results.
        source = draft
        rows = draft_rows_from_steps(source.payload.get('steps',{}),source.payload.get('asset_ids',[]))
    if (not source or source.state != 'succeeded' or source.owner_id != episode.owner_id
            or source.episode_id != episode.id
            or published_revision(source) != episode.evidence_revision):
        return
    if not isinstance(rows, list) or len(rows) > 1500:
        return
    for segment in segments:
        segment['_dialogue'] = []
        if segment.get('_preserve_expression'):
            continue
        if segment['asset_id'] not in source.payload.get('asset_ids', []):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get('asset_id') != segment['asset_id']:
                continue
            a,b,text = item.get('start'), item.get('end'), item.get('text')
            if (type(a) not in (int,float) or type(b) not in (int,float)
                    or not math.isfinite(a) or not math.isfinite(b)
                    or not segment['start']-.03 <= a < b <= segment['end']+.03
                    or not isinstance(text,str) or not text.strip() or len(text)>500
                    or any(ord(c)<32 for c in text)):
                continue
            text = display_text(text)
            if not text:
                continue
            cue = hashlib.sha256(f'draft:{draft.id}:{segment["asset_id"]}:{a:.6f}:{b:.6f}:{text}'.encode()).hexdigest()[:32]
            segment['_dialogue'].append({'start':a,'end':b,'text':text,'context':text,
                'cue_id':cue,'alignment':'draft','source_draft':True})
        segment['_dialogue'].sort(key=lambda row: (row['start'],row['end']))


def review_flags(row):
    text=row['text'];flags=[]
    if row.get('source_draft'):
        if not row.get('reviewed'):flags.append('自动字幕草稿，尚未核对')
    elif row.get('alignment')!='asr':flags.append('改字后沿用整句时间，请回听')
    if len(compact(text))>28:flags.append('句子偏长')
    if len(compact(text))/max(.1,row['end']-row['start'])>7:flags.append('显示偏快')
    if re.search(r'([\u3400-\u9fff]{2,4})\1',text):flags.append('有重复词，请听原声')
    if any(word in text for word in ('[音乐]','[噪声]','（笑声）','[笑声]')):flags.append('可能是环境声标记')
    return flags


def apply_subtitle_edits(segments, *, keep_hidden=False):
    from ..repository import DomainError
    for segment in segments:
        edits={item['cue_id']:item for item in segment.get('subtitle_edits',[])}
        rows=segment.get('_dialogue',[])
        segment['_speech_intervals']=[(row['start'],row['end']) for row in rows]
        if edits and segment.get('_preserve_expression'):
            raise DomainError('INTERVIEW_LOCKED','采访原话由本人确认，不能在这里改写字幕。',409)
        if not set(edits).issubset({row['cue_id'] for row in rows}):
            raise DomainError('SUBTITLES_CHANGED','选段或原声内容已经变化，请重新打开字幕校对。',409)
        for row in rows:
            row['original_text']=row['text'];row['reviewed']=False;row['hidden']=False
            if row['cue_id'] in edits:
                edit=edits[row['cue_id']]
                row.update(text=edit['text'],hidden=edit.get('hidden',False),reviewed=edit.get('reviewed',True))
                row['subtitle_changed']=row['hidden'] or row['text']!=row['original_text']
        if not keep_hidden:segment['_dialogue']=[row for row in rows if not row['hidden']]


def caption_timeline(segments, offset=0):
    result = []
    for segment, timing in zip(segments, timeline(segments)):
        for row in segment.get('_dialogue', []):
            start, end = float(row['start']), float(row['end'])
            if (not all(math.isfinite(x) for x in (start, end)) or
                    not segment['start'] - .03 <= start < end <= segment['end'] + .03):
                raise ValueError('Subtitle outside source segment')
            result.append({**row, 'start': offset + timing.start + max(start, segment['start']) - segment['start'],
                'end': offset + timing.start + min(end, segment['end']) - segment['start']})
    return result
