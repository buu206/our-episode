"""Bounded original-sound recuts, with explicit approval and immutable base."""
from copy import deepcopy
import shutil
import hashlib
import json
import re
import unicodedata

from ..models import Episode, Job, Version
from ..repository import DomainError
from .edit_contract import check_edit, is_director_edit, mark_edit
from .editing import EditSegment, timeline
from .render_review import fingerprint, validate


def eligible(db, version):
    return bool(version and version.state in ('ready', 'confirmed')
                and version.packaging == 'variety_v2'
                and not version.narration_text and not version.narration_path
                and 1 <= len(version.segments) <= 8 and is_director_edit(db, version))


def binding(repo, db, version):
    from ..worker import source_segments
    path = validate(repo, db, version)
    sources = source_segments(repo, db, version)
    episode = repo.owned(db, Episode, version.episode_id, version.owner_id)
    return {'evidence': hashlib.sha256(json.dumps(episode.evidence, sort_keys=True).encode()).hexdigest(),
            'output': fingerprint(path),
            'sources': {s['asset_id']: fingerprint(s['path']) for s in sources}}


def require_binding(repo, db, version, expected):
    if not eligible(db, version) or not expected or binding(repo, db, version) != expected:
        raise DomainError('CUT_PROPOSAL_STALE', '原片或当前版本已变化，请重新讨论剪法。', 409)


def requested_speech_ids(context):
    """Conservative explicit quoted retention, not general intent inference.

    Only latest question and confirmed utterances count. Ambiguous requests or
    unquoted paraphrases remain a model/preview responsibility.
    """
    question = (context or {}).get('question', '')
    def normalized(text):
        return ''.join(c for c in text if not c.isspace()
                       and not unicodedata.category(c).startswith('P'))
    ids = set()
    for match in re.finditer(r'[“「"]([^”」"\n]{2,160})[”」"]', question):
        prefix = re.split(r'[。！？；\n]', question[:match.start()])[-1][-100:]
        # Do not interpret a question, conditional or removal instruction as a
        # positive retention requirement. Prefer missing an ambiguous request
        # over inventing consent or suppressing a requested deletion.
        if (not re.search(r'保留|留住|留下|别删|不要删', prefix)
                or re.search(r'不用保留|不必保留|不要保留|不保留|不留|删掉|删除|去掉|舍弃|如果|是否|要不要', prefix)):
            continue
        quote = normalized(match.group(1))
        for row in (context or {}).get('confirmed_evidence', []):
            note = normalized(row.get('note', ''))
            if row.get('kind') == 'speech' and quote and quote in note and row.get('id'):
                ids.add(row['id'])
    return ids


def require_requested_speech(context, before, after):
    from .revision_review import compare
    protected = requested_speech_ids(context)
    if not protected:
        return
    report = compare(before, after, context.get('confirmed_evidence', []))
    if any(row['evidence_id'] in protected for row in report['affected_speech']):
        raise DomainError('REQUESTED_SPEECH_REMOVED',
                          '这条剪法删掉了你明确要求保留的对白，已拦截；原版不变。', 409)


def resolve(repo, db, version, selections, expected, requested_context=None):
    from ..worker import source_segments
    from .dialogue_captions import attach_dialogue
    require_binding(repo, db, version, expected)
    episode = repo.owned(db, Episode, version.episode_id, version.owner_id)
    sources = source_segments(repo, db, version)
    times = timeline(version.segments)
    segments, changes = [], []
    previous = (-1, 0)
    for selection in selections:
        index = selection['shot'] - 1
        start, end = selection['start'], selection['end']
        if not 0 <= index < len(sources):
            raise DomainError('INVALID_CUT_PROPOSAL', '建议引用的镜头不存在。', 422)
        original = version.segments[index]
        if (index < previous[0] or index == previous[0] and start < previous[1]
                or not original['start'] <= start < end <= original['end']):
            raise DomainError('INVALID_CUT_PROPOSAL', '只允许按原顺序保留现有镜头中的片段，不能倒置或重复反应。', 422)
        if sources[index].get('_preserve_expression') and (start != original['start'] or end != original['end']):
            raise DomainError('INTERVIEW_LOCKED', '本人确认的采访表达须完整保留。', 409)
        segment = {**deepcopy(original), 'start': start, 'end': end}
        if not segments:
            segment.update(transition='cut', transition_duration=0)
        # Existing reviewed text stays with retained utterances; edits belonging
        # solely to removed utterances do not become unrelated subtitle IDs.
        attach_dialogue(db, episode, [segment])
        ids = {row['cue_id'] for row in segment.pop('_dialogue', [])}
        if segment.get('subtitle_edits'):
            segment['subtitle_edits'] = [e for e in segment['subtitle_edits'] if e['cue_id'] in ids]
        try:
            EditSegment.model_validate(segment)
        except ValueError:
            raise DomainError('INVALID_CUT_PROPOSAL', '这个切点会截掉已有包装，请保留完整效果或另做拍法。', 422) from None
        segments.append(segment)
        changes.append({'shot': index+1, 'start': times[index].start+start-original['start'],
                        'end': times[index].start+end-original['start'], 'reason': selection['reason'], 'removed': False})
        previous = (index, end)
    retained = {s['shot']-1 for s in selections}
    if any(s.get('_preserve_expression') and i not in retained for i, s in enumerate(sources)):
        raise DomainError('INTERVIEW_LOCKED', '移除采访需要走采访修订，不能通过镜头调整绕过。', 409)
    check_edit(segments, episode)  # Includes whole speech, source grounding and transition guards.
    require_requested_speech(requested_context, version.segments, segments)
    before, after = times[-1].end, timeline(segments)[-1].end
    if after >= before - .03:
        raise DomainError('CUT_UNCHANGED', '这条建议没有实际缩短镜头，未创建重复候选。', 422)
    for index, original in enumerate(version.segments):
        cursor = original['start']
        kept = [s for s in selections if s['shot'] == index+1]
        for part in [*kept, {'start': original['end'], 'end': original['end']}]:
            if part['start'] - cursor > .03:
                changes.append({'shot': index+1, 'start': times[index].start+cursor-original['start'],
                                'end': times[index].start+part['start']-original['start'],
                                'reason': '这段不放入新候选，旧版仍保留', 'removed': True})
            cursor = part['end']
    changes.sort(key=lambda item: (item['shot'], item['start']))
    return segments, {'cut_changes': changes, 'before_seconds': before, 'after_seconds': after}


def create_candidate(repo, db, episode, version, turn):
    from ..worker import source_segments
    proposal = turn.proposal
    segments, public = resolve(repo, db, version, proposal['selections'], proposal['cut_binding'], turn.context)
    if any(proposal.get(key) != value for key, value in public.items()):
        raise DomainError('CUT_PROPOSAL_STALE', '镜头建议已变化，请重新查看。', 409)
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise DomainError('MEDIA_UNAVAILABLE', '媒体服务尚未就绪，未创建新版本。', 503)
    new = Version(owner_id=episode.owner_id, episode_id=episode.id,
        title=(version.title+' · 镜头调整')[:120], origin=version.origin,
        segments=segments, base_version_id=version.id, evidence_revision=version.evidence_revision,
        direction_id=version.direction_id, narration_text='', packaging=version.packaging)
    db.add(new); db.flush()
    mark_edit(repo, db, new)
    source_segments(repo, db, new)
    job = Job(owner_id=episode.owner_id, episode_id=episode.id, version_id=new.id,
              kind='render', idempotency_key='director-cut:'+turn.id)
    db.add(job); db.flush()
    new.state, new.job_id = 'rendering', job.id
    from .director_run import create
    from .director_guidance import revision
    create(repo, db, new, job, revision(db, episode), approved_proposal_id=turn.id)
    repo.audit(db, episode.owner_id, 'cut_candidate_created', new.id, base_version_id=version.id)
    return {'ok': True, 'next_step': 'preview', 'version_id': new.id, 'job_id': job.id}
