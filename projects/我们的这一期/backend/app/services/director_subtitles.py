"""A bounded, explicitly accepted subtitle repair; never changes the base cut."""
from copy import deepcopy
import shutil

from ..models import Job, Version
from ..repository import DomainError
from . import audio_review, media


def resolve(repo, db, version, context, corrections, expected_snapshot=None):
    from ..worker import source_segments
    if not version or version.packaging != 'variety_v2' or version.state not in ('ready', 'confirmed'):
        raise DomainError('SUBTITLE_PROPOSAL_STALE', '请在可播放的综艺版本上重新讨论字幕。', 409)
    current = audio_review.context(repo, db, version, (context.get('audio_check') or {}).get('focus_cue_id'))
    if not current or current != context.get('audio_check'):
        raise DomainError('SUBTITLE_PROPOSAL_STALE', '对白检查已变化，请让导演按当前结果重新建议。', 409)
    try:
        bound = audio_review.snapshot(repo, db, version)
    except media.MediaError:
        raise DomainError('SUBTITLE_PROPOSAL_STALE', '声音文件已变化，旧字幕建议不会执行。', 409) from None
    if expected_snapshot is not None and bound != expected_snapshot:
        raise DomainError('SUBTITLE_PROPOSAL_STALE', '成片或来源已变化，旧字幕建议不会执行。', 409)
    sources = source_segments(repo, db, version, subtitle_review=True)
    segments = deepcopy(version.segments)
    changes = []
    for correction in corrections:
        number = correction['finding']
        if not 1 <= number <= len(current['findings']):
            raise DomainError('INVALID_SUBTITLE_PROPOSAL', '字幕建议没有对应的检查项。', 409)
        finding = current['findings'][number-1]
        matches = [(index, row) for index, segment in enumerate(sources)
                   for row in segment.get('_dialogue', []) if row['cue_id'] == finding['cue_id']]
        if (len(matches) != 1 or matches[0][1].get('hidden')
                or matches[0][1]['text'] != finding['caption']):
            raise DomainError('SUBTITLE_PROPOSAL_STALE', '无法唯一定位这句字幕，请在校对字幕中处理。', 409)
        index, row = matches[0]
        if sources[index].get('_preserve_expression'):
            raise DomainError('SUBTITLE_EXPRESSION_LOCKED', '这是本人确认的采访表达，请通过采访修订处理。', 409)
        if row['text'] == correction['text']:
            raise DomainError('INVALID_SUBTITLE_PROPOSAL', '建议没有改变字幕，未创建重复版本。', 409)
        edits = [e for e in segments[index].get('subtitle_edits', []) if e['cue_id'] != row['cue_id']]
        edits.append(dict(cue_id=row['cue_id'], text=correction['text'], hidden=False, reviewed=True))
        segments[index]['subtitle_edits'] = edits
        changes.append(dict(before=row['text'], after=correction['text'], start=finding['start'], end=finding['end']))
    # Use the same render contract as manual subtitle edits, including controls.
    from .editing import EditSegment
    try:
        for segment in segments:
            EditSegment.model_validate(segment)
    except ValueError:
        raise DomainError('INVALID_SUBTITLE_PROPOSAL', '建议字幕未通过渲染格式检查。', 409) from None
    return segments, changes, bound


def create_candidate(repo, db, episode, version, turn):
    from ..worker import source_segments
    from .edit_contract import is_director_edit, mark_edit
    from .packaging import reusable_narration
    proposal = turn.proposal
    segments, changes, _ = resolve(repo, db, version, turn.context, proposal['corrections'], proposal['audio_snapshot'])
    if changes != proposal['subtitle_changes']:
        raise DomainError('SUBTITLE_PROPOSAL_STALE', '修改内容已变化，请重新查看建议。', 409)
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise DomainError('MEDIA_UNAVAILABLE', '媒体服务尚未就绪，未创建新版本。', 503)
    voice = None
    if version.narration_text:
        if not version.narration_approved or not version.narration_path or version.error:
            raise DomainError('NARRATION_REQUIRED', '原版旁白尚不可复用，请先核对声音。', 409)
        try:
            reusable_narration(repo.data_dir, version.narration_path)
        except media.MediaError as exc:
            raise DomainError(exc.code, exc.message, 409) from None
        voice = version.narration_path
    new = Version(owner_id=episode.owner_id, episode_id=episode.id,
        title=(version.title + ' · 导演字幕调整')[:120], origin=version.origin,
        segments=segments, base_version_id=version.id, evidence_revision=version.evidence_revision,
        direction_id=version.direction_id, narration_text=version.narration_text,
        narration_path=voice, narration_approved=bool(voice), packaging=version.packaging)
    db.add(new); db.flush()
    if is_director_edit(db, version):
        mark_edit(repo, db, new)
    source_segments(repo, db, new)
    job = Job(owner_id=episode.owner_id, episode_id=episode.id, version_id=new.id,
        kind='render', idempotency_key='director-subtitles:' + turn.id)
    db.add(job); db.flush()
    new.state, new.job_id = 'rendering', job.id
    from .director_run import create
    from .director_guidance import revision
    create(repo, db, new, job, revision(db, episode), approved_proposal_id=turn.id)
    repo.audit(db, episode.owner_id, 'subtitle_candidate_created', new.id, base_version_id=version.id)
    return {'ok': True, 'next_step': 'preview', 'version_id': new.id, 'job_id': job.id}
