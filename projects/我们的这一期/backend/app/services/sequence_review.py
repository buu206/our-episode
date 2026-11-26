"""Ordered, bounded output snapshots. Observations are hypotheses, never edit approval."""
import base64
import hashlib
import json
import math
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from ..models import Episode, Job, Version
from ..repository import DomainError
from . import media, render_review, visual_review
from .director import DirectorError, _invalid_constant, _unique_object
from .material_trial import digest
from .vision import _jpeg_frame, _check_jpeg

PROMPT = Path(__file__).with_name('prompts') / 'sequence_review.txt'
REVISION = 'sequence-review-v1'
MAX_FRAMES = 24
RESERVATION = .10
SAMPLING = 'short-window-4fps-v1'


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    first_frame: str = Field(min_length=1, max_length=8)
    last_frame: str = Field(min_length=1, max_length=8)
    visible_change: str = Field(min_length=1, max_length=80)
    uncertainty: str = Field(min_length=1, max_length=80)

    @field_validator('visible_change', 'uncertainty')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank observation')
        return value.strip()


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    summary: str = Field(min_length=1, max_length=100)
    observations: list[Observation] = Field(max_length=3)

    @field_validator('summary')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank summary')
        return value.strip()


def parse(raw, frames):
    try:
        result = Review.model_validate(json.loads(raw, object_pairs_hook=_unique_object,
                                                 parse_constant=_invalid_constant))
        positions = {f['frame_id']: f['time'] for f in frames}
        pairs = [(o.first_frame, o.last_frame) for o in result.observations]
        if len(set(pairs)) != len(pairs) or any(
                a not in positions or b not in positions or positions[a] >= positions[b] for a, b in pairs):
            raise ValueError('invalid ordered pair')
        return result.model_dump()
    except (ValueError, TypeError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '局部过程观察未通过时间位置与结构检查。') from None


def snapshot(repo, db, version, window, *, include_speech=False, sampling=None):
    start, end = window['start'], window['end']
    if not all(type(t) in (float, int) and math.isfinite(t) for t in (start, end)) or not 0 <= start < end or end-start > 24:
        raise DomainError('REVIEW_WINDOW_INVALID', '每次查看0到90秒内、不超过24秒的一段。', 422)
    bound = visual_review.snapshot(repo, db, version, operation='sequence_review')
    duration = media.probe(repo.path(version.output_path))['duration']
    if end > duration or end > media.MAX_OUTPUT_SECONDS:
        raise DomainError('REVIEW_WINDOW_INVALID', '所选区间超出当前成片时长。', 422)
    result = {**bound, 'prompt_sha256': render_review.fingerprint(PROMPT), 'window': window}
    if sampling is not None:
        if sampling != SAMPLING:
            raise DomainError('REVIEW_SAMPLING_INVALID', '动作观察采样规则无法核对，未重新发送。', 409)
        result['sampling'] = sampling
    if include_speech:
        from . import sequence_dialogue
        episode = repo.owned(db, Episode, version.episode_id, version.owner_id)
        speech = sequence_dialogue.packet(episode, version, window)
        if speech is None:
            raise DomainError('SPEECH_CONTEXT_CHANGED', '已确认对白或成片时间对应已变化，本次观察停止。', 409)
        result.update(speech_digest=digest(speech),
                      prompt_sha256=render_review.fingerprint(sequence_dialogue.PROMPT))
    return result


def validate_job(repo, db, job):
    version = repo.owned(db, Version, job.version_id, job.owner_id)
    if snapshot(repo, db, version, job.payload['window'],
                include_speech='speech_digest' in job.payload['snapshot'],
                sampling=job.payload['snapshot'].get('sampling')) != job.payload['snapshot']:
        raise DomainError('REVIEW_OUTPUT_CHANGED', '成片、来源或审片规则已变化，本次过程观察已停止。', 409)
    return repo.owned(db, Episode, job.episode_id, job.owner_id), []


def enqueue(repo, db, version, services, window):
    visual_review.check_config(services.config)
    window = {k: float(window[k]) for k in ('start', 'end')}
    bound = snapshot(repo, db, version, window, sampling=SAMPLING)
    from . import sequence_dialogue
    episode = repo.owned(db, Episode, version.episode_id, version.owner_id)
    if sequence_dialogue.packet(episode, version, window):
        bound = snapshot(repo, db, version, window, include_speech=True, sampling=SAMPLING)
    key = REVISION + ':' + digest(bound)
    old = db.scalar(select(Job).where(Job.owner_id == version.owner_id, Job.idempotency_key == key))
    if old:
        return old
    job = Job(owner_id=version.owner_id, episode_id=version.episode_id, version_id=version.id,
              kind='sequence_review', idempotency_key=key, payload={'snapshot': bound, 'window': window},
              stage='等待查看局部动作过程')
    db.add(job); db.flush()
    return job


def prepare(repo, job_id):
    with repo.factory() as db:
        job = db.get(Job, job_id); validate_job(repo, db, job)
        path = repo.path(db.get(Version, job.version_id).output_path)
        start, end = job.payload['window']['start'], job.payload['window']['end']
        sampling = job.payload['snapshot'].get('sampling')
    rate = 4 if sampling == SAMPLING and end - start <= 6 else 1
    count = min(MAX_FRAMES, max(2, math.ceil((end-start)*rate)+1))
    positions = [start + (i+.5)*(end-start)/count for i in range(count)]
    if sampling == SAMPLING:
        # Seek to actual video frames, not a nominal timestamp after the last
        # frame near EOF. Two labels must never stand for the same frame.
        raw = media._run('ffprobe', ['-v', 'error', '-protocol_whitelist', 'file,pipe',
            '-format_whitelist', 'mov', '-select_streams', 'v:0', '-show_frames',
            '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', str(path)],
            timeout=60, code='MEDIA_DECODE_FAILED')
        try:
            rows = json.loads(raw)['frames']
            if not isinstance(rows, list) or len(rows) > 6000:
                raise ValueError('frame count')
            available = sorted({float(row['best_effort_timestamp_time']) for row in rows})
            if any(not math.isfinite(t) or t < 0 for t in available):
                raise ValueError('frame timestamp')
            available = [t for t in available if start < t < end]
        except (ValueError, TypeError, KeyError):
            raise DomainError('FRAME_TIMESTAMPS_INVALID', '无法核对这段视频的实际帧位置，未发送模型。', 409) from None
        if len(available) < 2:
            raise DomainError('REVIEW_WINDOW_TOO_SHORT', '这段不足两张不同画面，请稍微扩大查看范围。', 422)
        positions = sorted({min(available, key=lambda t: abs(t-wanted)) for wanted in positions})
        if len(positions) < 2:
            raise DomainError('REVIEW_WINDOW_TOO_SHORT', '这段不足两张不同画面，请稍微扩大查看范围。', 422)
    frames = []
    for i, when in enumerate(positions):
        # A tiny lead avoids decimal rounding past the frame's exact PTS.
        seek = max(0., when - .00001) if sampling == SAMPLING else when
        jpeg = _jpeg_frame(path, seek); _check_jpeg(jpeg)
        frames.append({'frame_id': f's{i+1}', 'time': round(when, 6), 'jpeg': jpeg})
    with repo.factory() as db:
        validate_job(repo, db, db.get(Job, job_id))
    return frames


class Adapter(visual_review.Adapter):
    async def review(self, frames, reservation, speech=None):
        from . import sequence_dialogue
        content = [{'type': 'text', 'text': (sequence_dialogue.PROMPT if speech else PROMPT).read_text()}]
        if speech:
            content.append({'type': 'text', 'text': json.dumps(
                {'untrusted_confirmed_speech': sequence_dialogue.wire(speech)}, ensure_ascii=False)})
        for f in frames:
            content.extend([{'type': 'text', 'text': json.dumps({'frame_id': f['frame_id'], 'time': f['time']})},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' +
                 base64.b64encode(f['jpeg']).decode(), 'detail': 'original'}}])
        if sum(len(c['text'].encode()) for c in content if c['type']=='text') + 2048 + len(frames)*1024 > self.input_limit:
            raise DirectorError('INPUT_LIMIT', '局部过程抽样超过本轮预留范围。', usage=visual_review.empty_usage())
        raw, usage = await self.request([{'role': 'user', 'content': content}], reservation)
        try:
            return {'result': sequence_dialogue.parse_model(raw, frames, speech) if speech else parse(raw, frames), 'usage': usage}
        except DirectorError as exc:
            reason = (exc.usage or {}).get('validation_reason')
            exc.usage = {**usage, **({'validation_reason': reason} if reason else {})}
            raise


def run(pipeline, job_id):
    from .pipeline import Pipeline
    repo, upstream = pipeline.repo, pipeline.services
    def recheck():
        with repo.factory() as db:
            validate_job(repo, db, db.get(Job, job_id))
    class ScopedServices:
        is_real = upstream.is_real
        def ready(self):
            try:
                visual_review.check_config(upstream.config); recheck()
                return True, '仅观察已授权成片的指定区间。'
            except (DirectorError, DomainError) as exc:
                return False, exc.message
        def profile(self):
            with repo.factory() as db:
                bound = db.get(Job, job_id).payload['snapshot']
            return {'scope': REVISION, 'model': visual_review.MODEL, 'snapshot_digest': digest(bound)}
    scoped = Pipeline(repo, ScopedServices())
    frames = prepare(repo, job_id)
    from . import sequence_dialogue
    with repo.factory() as db:
        job = db.get(Job, job_id); validate_job(repo, db, job)
        speech = sequence_dialogue.packet(db.get(Episode, job.episode_id), db.get(Version, job.version_id),
            job.payload['window']) if 'speech_digest' in job.payload['snapshot'] else None
    frame_packet = [{'frame_id': f['frame_id'], 'time': f['time'],
                     'sha256': hashlib.sha256(f['jpeg']).hexdigest()} for f in frames]
    packet = digest({'frames': frame_packet, 'speech': speech}) if speech else digest(frame_packet)
    result = scoped.invoke(job_id, 'visual_review', RESERVATION,
        lambda reservation, permit: Adapter(upstream.config, recheck, budget_check=permit).review(frames, reservation, speech),
        request_digest=packet, checkpoint_key='sequence_review')
    result = sequence_dialogue.parse(json.dumps(result), frames, speech) if speech else parse(json.dumps(result), frames)
    positions = {f['frame_id']: f['time'] for f in frames}
    with repo.transaction() as db:
        job = db.get(Job, job_id); validate_job(repo, db, job)
        if job.state != 'running':
            raise DomainError('JOB_INTERRUPTED', '过程观察已停止。', 409)
        report = {**result, 'version_id': job.version_id, 'scope': 'sampled_sequence',
            'verification': 'unverified', 'audio_analyzed': False, 'window': job.payload['window'],
            'output_sha256': job.payload['snapshot']['output_sha256'],
            'sample_times': list(positions.values()),
            'observations': [{**o, 'start': positions[o['first_frame']], 'end': positions[o['last_frame']]}
                             for o in result['observations']],
            'not_checked': ['采样间动作及完整连续视频', '声音、人物意图和动作因果', '可安全删除的镜头区间']}
        if job.payload['snapshot'].get('sampling'):
            report['sampling'] = {'strategy': SAMPLING, 'frame_count': len(frames),
                'max_sample_gap_seconds': round(max(b['time']-a['time'] for a,b in zip(frames, frames[1:])), 6),
                'continuous_video': False}
        if speech:
            used = set()
            for observation in report['observations']:
                related = sequence_dialogue.overlapping(speech, observation['start'], observation['end'])
                observation['speech_evidence_ids'] = list(dict.fromkeys(row['evidence_id'] for row in related))
                used.update(observation['speech_ids'])
            report['speech_context'] = {'scope': speech['scope'], 'alignment_method': 'deterministic_time_overlap',
                'count': len(speech['items']),
                'omitted': speech['omitted'], 'unlinked_evidence_ids': list(dict.fromkeys(
                    row['evidence_id'] for row in speech['items'] if row['speech_id'] not in used))}
        scoped.finish(db, job, report, '局部动作观察已完成，尚非剪辑结论')


def context(repo, db, version):
    """Keep distinct valid windows together; a new look must not erase an old one."""
    if version is None or version.state not in ('ready', 'confirmed'):
        return None
    jobs = list(db.scalars(select(Job).where(Job.owner_id == version.owner_id,
        Job.version_id == version.id, Job.kind == 'sequence_review', Job.state == 'succeeded')
        .order_by(Job.created_at.desc(), Job.id.desc()).limit(33)))
    if not jobs:
        return None
    windows, seen, omitted = [], set(), 0
    for job in jobs[:32]:
        if not job.result:
            continue
        try:
            validate_job(repo, db, job)
        except (DomainError, DirectorError, OSError, ValueError):
            continue
        window = job.payload['window']
        key = (window['start'], window['end'])
        if key in seen:
            continue
        seen.add(key)
        if len(windows) >= 4:
            omitted += 1
            continue
        projected = {'window': window, 'summary': job.result['summary'],
            'observations': [{k: o[k] for k in ('start', 'end', 'visible_change', 'uncertainty', 'speech_evidence_ids') if k in o}
                             for o in job.result['observations']]}
        if job.result.get('speech_context'):
            projected['speech_context'] = job.result['speech_context']
        if job.result.get('sampling'):
            projected['sampling'] = job.result['sampling']
        windows.append(projected)
    if not windows:
        return None
    windows.sort(key=lambda w: (w['window']['start'], w['window']['end']))
    duration = media.probe(repo.path(version.output_path))['duration']
    gaps, cursor = [], 0.
    for row in windows:
        start, end = row['window']['start'], row['window']['end']
        if start > cursor:
            gaps.append({'start': round(cursor, 3), 'end': round(start, 3)})
        cursor = max(cursor, end)
    if cursor < duration:
        gaps.append({'start': round(cursor, 3), 'end': round(duration, 3)})
    return {'scope': 'sampled_sequences', 'verification': 'unverified', 'audio_analyzed': False,
        'windows': windows, 'reports_omitted': omitted, 'older_reports_not_loaded': len(jobs) > 32,
        'unobserved_windows': gaps,
        'not_checked': ['各窗口内仍仅抽帧，非连续动作验证', '声音、人物意图和动作因果', '可安全删除的镜头区间']}


def binding(repo, db, version):
    bound = visual_review.snapshot(repo, db, version)
    from . import sequence_dialogue
    episode = repo.owned(db, Episode, version.episode_id, version.owner_id)
    return {**bound, 'prompt_sha256': render_review.fingerprint(PROMPT),
        'dialogue_prompt_sha256': render_review.fingerprint(sequence_dialogue.PROMPT),
        'speech_revision_digest': digest({'evidence': episode.evidence, 'state': episode.analysis_state,
                                        'confirmed': episode.evidence_confirmed_revision})}


def require_binding(repo, db, version, expected):
    if not expected or binding(repo, db, version) != expected:
        raise DomainError('REVIEW_OUTPUT_CHANGED', '成片或查看范围的授权已变化，请重新讨论。', 409)


def capability(repo, db, version):
    if not version or version.state not in ('ready', 'confirmed'):
        return None
    try:
        binding(repo, db, version)
        return {'duration': media.probe(repo.path(version.output_path))['duration'], 'max_seconds': 24}
    except (DomainError, DirectorError, OSError, ValueError):
        return None


def proposal_status(repo, db, version, proposal):
    job_id = proposal.get('result', {}).get('job_id')
    job = db.get(Job, job_id) if job_id else None
    if (not job or job.owner_id != version.owner_id or job.version_id != version.id
            or job.kind != 'sequence_review'):
        return {'review_state': 'stale', 'review_summary': '观察记录已不可用。'}
    try:
        validate_job(repo, db, job)
    except (DomainError, DirectorError, OSError, ValueError):
        return {'review_state': 'stale', 'review_summary': '成片或授权已更新，请重新查看。'}
    if job.state == 'succeeded':
        return {'review_state': 'succeeded', 'review_summary': (job.result or {}).get('summary', '观察已结束。')}
    if job.state in ('queued', 'running'):
        return {'review_state': job.state}
    return {'review_state': 'failed', 'review_summary': (job.error or {}).get('message', '这次没有看完，未自动重试或改片。')}
