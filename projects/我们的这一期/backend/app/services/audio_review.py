"""Re-transcribe actual output audio; differences are suggestions, never edits."""
from copy import deepcopy
from difflib import SequenceMatcher
import math
import unicodedata

from sqlalchemy import select

from ..models import Episode, Job, Version
from ..repository import DomainError
from . import media, render_review
from .artifacts import artifact_workspace
from .dialogue_captions import caption_timeline
from .material_trial import GrantedASR, digest, key_guard, signature
from .minimax_asr import MODEL, asr_estimate
from .speech import extract_audio_chunks

REVISION = 'audio-review-v1'


def expected_captions(repo, db, version):
    from ..worker import source_segments
    from .packaging import _narration
    if version.packaging != 'variety_v2':
        raise DomainError('SUBTITLE_STYLE_REQUIRED', '请在带对白字幕的综艺节奏版本中核对。', 409)
    segments = source_segments(repo, db, version)
    offset = 0.
    if version.narration_path:
        pcm, rate, channels = _narration(repo.path(version.narration_path))
        offset = math.ceil(len(pcm) / (rate * channels * 2) * 30 - 1e-8) / 30 + 2 / 30
    cues = caption_timeline(segments, offset)
    if not cues:
        raise DomainError('SUBTITLES_MISSING', '这版还没有可对照的对白字幕，请先确认原声文字。', 409)
    return [{k: c[k] for k in ('cue_id', 'text', 'start', 'end')} for c in cues]


def snapshot(repo, db, version):
    # Reuse the same explicit three-source grant and current-output checks.
    from .visual_review import snapshot as visual_snapshot
    bound = visual_snapshot(repo, db, version, operation='audio_review')
    bound.pop('prompt_sha256')
    bound['audio_review_revision'] = REVISION
    bound['caption_digest'] = digest(expected_captions(repo, db, version))
    return bound


def validate_job(repo, db, job):
    version = repo.owned(db, Version, job.version_id, job.owner_id)
    if snapshot(repo, db, version) != job.payload['snapshot']:
        raise DomainError('REVIEW_OUTPUT_CHANGED', '成片、字幕或来源已变化，本次对白核对已停止。', 409)
    return repo.owned(db, Episode, job.episode_id, job.owner_id), []


def check_config(config):
    key_guard(config.get('MINIMAX_API_KEY'))
    if config.get('MINIMAX_ASR_MODEL', MODEL) != MODEL:
        raise DomainError('MODEL_NOT_ALLOWED', '对白核对仅使用当前已适配的MiniMax转写配置。', 409)


def enqueue(repo, db, version, services):
    check_config(services.config)
    bound = snapshot(repo, db, version)
    key = REVISION + ':' + digest(bound)
    existing = db.scalars(select(Job).where(Job.owner_id == version.owner_id, Job.idempotency_key == key)).first()
    if existing:
        return existing  # Includes failures and uncertain charges; no blind retries.
    job = Job(owner_id=version.owner_id, episode_id=version.episode_id, version_id=version.id,
              kind='audio_review', idempotency_key=key, payload={'snapshot': bound}, stage='等待核对成片对白')
    db.add(job); db.flush()
    return job


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def compare(cues, words, duration):
    findings = []
    for cue in cues:
        target = normalized(cue['text'])
        if not target:
            continue
        overlapping = [w for w in words if min(w['end'], cue['end']) - max(w['start'], cue['start'])
                       >= (w['end'] - w['start']) * .5]
        heard = ''.join(w['text'] for w in overlapping)
        if normalized(heard) == target:
            continue
        # A word/utterance straddling cue boundaries cannot reliably be split.
        boundary = any(w['start'] < cue['start'] - .25 or w['end'] > cue['end'] + .25 for w in overlapping)
        code = 'ASR_BOUNDARY_UNCERTAIN' if boundary else 'CAPTION_AUDIO_DIFFERENCE' if heard else 'ASR_NO_MATCH'
        message = {'ASR_BOUNDARY_UNCERTAIN': '识别时间跨过了字幕边界，先回听确认断句。',
                   'CAPTION_AUDIO_DIFFERENCE': '这句字幕和重新识别的声音有差异，听原声再决定改哪几个字。',
                   'ASR_NO_MATCH': '这句字幕附近没有识别到对应的话，也可能是声音太轻或识别漏了。'}[code]
        findings.append({'code': code, 'cue_id': cue['cue_id'],
            'start': round(max(0., cue['start'] - .25), 3), 'end': round(min(duration, cue['end'] + .25), 3),
            'caption': cue['text'][:160], 'recognized': heard[:160],
            'similarity': round(SequenceMatcher(None, target, normalized(heard), autojunk=False).ratio(), 3),
            'message': message})
    return {'scope': 'output_audio_asr', 'verification': 'unverified',
        'captions_checked': len(cues), 'issue_count': len(findings), 'findings': findings[:8],
        'findings_omitted': max(0, len(findings)-8),
        'not_checked': ['ASR本身是否听错', '画面中实际烧录的字是否一致', '口型同步', '故事与综艺效果']}


def checked_words(value, asset_id, audio_seconds):
    if not isinstance(value, list) or len(value) > 4000:
        raise DomainError('INVALID_ASR_RESULT', '对白核对的识别结果无效。', 409)
    previous = -1.
    for row in value:
        if (not isinstance(row, dict) or row.get('asset_id') != asset_id
                or not isinstance(row.get('text'), str) or not row['text'].strip() or len(row['text']) > 1000
                or any(type(row.get(k)) not in (int, float) or not math.isfinite(row[k]) for k in ('start','end'))
                or not 0 <= row['start'] < row['end'] <= audio_seconds + .001 or row['start'] < previous):
            raise DomainError('INVALID_ASR_RESULT', '对白核对的时间或文字无效。', 409)
        previous = row['start']
    return deepcopy(value)


def run(pipeline, job_id):
    from .pipeline import Pipeline
    from .playback_review import inspect_audio_span
    repo, upstream = pipeline.repo, pipeline.services
    def recheck():
        check_config(upstream.config)
        with repo.factory() as db:
            validate_job(repo, db, db.get(Job, job_id))
    class ScopedServices:
        is_real = upstream.is_real
        def ready(self):
            recheck()
            return True, '仅本期已授权素材生成的成片音轨。'
        def profile(self):
            with repo.factory() as db:
                return {'scope': REVISION, 'model': MODEL,
                        'snapshot_digest': digest(db.get(Job, job_id).payload['snapshot'])}
    scoped = Pipeline(repo, ScopedServices())
    with repo.factory() as db:
        job = db.get(Job, job_id); validate_job(repo, db, job)
        version = db.get(Version, job.version_id)
        path = repo.path(version.output_path)
        cues = expected_captions(repo, db, version)
        vid = version.id
    info = media.probe(path)
    if not info['has_audio'] or not 0 < info['duration'] <= media.MAX_OUTPUT_SECONDS + .15:
        raise DomainError('AUDIO_REVIEW_UNAVAILABLE', '这版需要有音轨且不超过90秒，才能核对对白。', 409)
    span = inspect_audio_span(path, info['duration'])
    if not span['checked']:
        raise DomainError('AUDIO_TIMING_UNKNOWN', '无法确定声音位置，先完成本机播放检查。', 409)
    with artifact_workspace(repo, job_id) as directory:
        chunks = extract_audio_chunks(path, directory, vid)
        seconds = sum(c.duration for c in chunks)
        if abs(seconds - (span['end'] - span['start'])) > .15:
            raise DomainError('AUDIO_TIMING_UNKNOWN', '解码声音时长与成片音轨不一致，未发送。', 409)
        bound = signature(chunks)
        recheck()
        result = scoped.invoke(job_id, 'asr', asr_estimate(chunks),
            lambda reservation, permit: GrantedASR(upstream.config['MINIMAX_API_KEY'], bound, recheck,
                budget_check=permit).transcribe(chunks, reservation=reservation),
            request_digest=digest(bound), checkpoint_key='output_audio_asr')
        words = checked_words(result, vid, seconds)
        for row in words:
            row['start'], row['end'] = max(0., row['start'] + span['start']), min(info['duration'], row['end'] + span['start'])
        words = [r for r in words if r['end'] > r['start']]
        report = compare(cues, words, info['duration'])
        with repo.transaction() as db:
            job = db.get(Job, job_id); validate_job(repo, db, job)
            if job.state != 'running':
                raise DomainError('JOB_INTERRUPTED', '对白核对已停止。', 409)
            scoped.finish(db, job, {**report, 'version_id': vid, 'duration': info['duration'],
                'audio_seconds': seconds, 'model': MODEL,
                'output_sha256': job.payload['snapshot']['output_sha256']}, '成片对白核对完成，差异待回听')


def context(repo, db, version, focus_cue_id=None):
    if version is None or version.state not in ('ready', 'confirmed'):
        return None
    job = db.scalars(select(Job).where(Job.owner_id == version.owner_id, Job.version_id == version.id,
        Job.kind == 'audio_review', Job.state == 'succeeded').order_by(Job.created_at.desc())).first()
    if not job or not job.result:
        return None
    try:
        validate_job(repo, db, job)
    except (DomainError, OSError, ValueError, media.MediaError):
        return None
    # The full report stays persisted and visible in the preview. Give the
    # director a bounded shortlist, retaining explicit coverage and uncertainty.
    report = job.result
    findings = report['findings']
    if focus_cue_id:
        selected = [f for f in findings if f['cue_id'] == focus_cue_id]
        if len(selected) != 1:
            raise DomainError('AUDIO_FOCUS_STALE', '这句已不在当前对白检查中，请回到预览重新选择。', 409)
        findings = selected + [f for f in findings if f['cue_id'] != focus_cue_id]
    return {k: deepcopy(report[k]) for k in ('scope', 'verification', 'captions_checked', 'issue_count', 'not_checked')} | {
        'findings': [{k: f[k] for k in ('code', 'cue_id', 'start', 'end', 'caption', 'recognized')}
                     for f in findings[:3]],
        'findings_omitted': report['issue_count'] - min(3, len(findings))} | (
            {'focus_cue_id': focus_cue_id} if focus_cue_id else {})
