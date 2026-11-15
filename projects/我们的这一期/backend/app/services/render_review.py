"""Local, bounded inspection of an actual output, never a story-quality verdict."""
from array import array
import hashlib
import json
import math
import sys

from sqlalchemy import select
from ..models import Asset, Episode, Job, Version
from ..repository import DomainError
from . import media

REVISION = 'technical-review-v3'


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def version_digest(version):
    value = {key: getattr(version, key) for key in (
        'id', 'output_path', 'segments', 'narration_text', 'narration_path',
        'packaging', 'evidence_revision')}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate(repo, db, version):
    from ..worker import source_segments
    source_segments(repo, db, version)
    if version.state not in ('ready', 'confirmed') or not version.output_path:
        raise DomainError('REVIEW_NOT_READY', '先生成这一版，再检查成片。', 409)
    path = repo.path(version.output_path)
    if not path.is_file():
        raise DomainError('OUTPUT_MISSING', '成片文件暂不可用，请重新生成。', 409)
    return path


def enqueue(repo, db, version):
    path = validate(repo, db, version)
    digest = version_digest(version)
    key = REVISION + ':' + digest
    existing = db.scalars(select(Job).where(Job.owner_id == version.owner_id,
                                           Job.idempotency_key == key)).first()
    if existing:
        # A local check can be explicitly retried; it never calls a supplier.
        stale = (existing.state == 'succeeded' and
                 (existing.result or {}).get('output_sha256') != fingerprint(path))
        if existing.state == 'failed' or stale:
            existing.state, existing.stage, existing.error = 'queued', '等待重新检查成片', None
            existing.result = None
        return existing
    job = Job(owner_id=version.owner_id, episode_id=version.episode_id,
              version_id=version.id, kind='technical_review', idempotency_key=key,
              payload={'version_digest': digest}, stage='等待检查画面与声音')
    db.add(job)
    db.flush()
    return job


def inspect_output(path, *, expect_audio):
    before = fingerprint(path)
    info = media.probe(path)
    duration = info['duration']
    if duration > media.MAX_OUTPUT_SECONDS + .15:
        raise DomainError('REVIEW_DURATION_LIMIT', '成片超出本轮90秒上限。', 409)
    # Decode every video/audio packet. Metadata and a single frame are insufficient.
    media._run('ffmpeg', ['-nostdin', '-v', 'error', '-xerror', '-threads', '2',
        '-i', str(path), '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'],
        timeout=120, code='MEDIA_DECODE_FAILED')
    issues = []
    audio = {'has_track': info['has_audio'], 'measured_seconds': 0.0,
             'rms_dbfs': None, 'peak_dbfs': None}
    if expect_audio and not info['has_audio']:
        issues.append({'code': 'AUDIO_MISSING', 'start': 0.0, 'end': duration,
                       'message': '这版没有音轨，原声或旁白可能没有合进去。'})
    if info['has_audio']:
        raw = media._run('ffmpeg', ['-nostdin', '-v', 'error', '-threads', '2',
            '-i', str(path), '-map', '0:a:0', '-vn', '-t', '90.15',
            '-ac', '1', '-ar', '8000', '-f', 's16le', '-'], timeout=60,
            code='MEDIA_DECODE_FAILED')
        samples = array('h', raw)
        if sys.byteorder != 'little':
            samples.byteswap()
        if samples:
            rms = math.sqrt(sum(n*n for n in samples) / len(samples)) / 32768
            peak = max(abs(n) for n in samples) / 32768
            dbfs = lambda value: round(20 * math.log10(max(value, 1e-6)), 1)
            audio.update(measured_seconds=round(len(samples)/8000, 3),
                         rms_dbfs=dbfs(rms), peak_dbfs=dbfs(peak))
            # Only flag complete >=1s windows. A pause is a review hint, not an error.
            quiet = []
            for start in range(0, len(samples)-7999, 8000):
                chunk = samples[start:start+8000]
                if math.sqrt(sum(n*n for n in chunk)/len(chunk))/32768 < .00178:
                    if quiet and quiet[-1][1] == start/8000:
                        quiet[-1][1] += 1
                    else:
                        quiet.append([start/8000, start/8000+1])
            if expect_audio:
                for start, end in quiet[:8]:
                    issues.append({'code': 'QUIET_INTERVAL', 'start': start, 'end': end,
                        'message': '这里声音很轻，也可能是自然停顿，建议回听。'})
        elif expect_audio:
            issues.append({'code': 'AUDIO_EMPTY', 'start': 0.0, 'end': duration,
                           'message': '音轨没有解码出声音，请检查合成结果。'})
    if before != fingerprint(path):
        raise DomainError('REVIEW_OUTPUT_CHANGED', '成片已变化，请检查当前版本。', 409)
    return {'revision': REVISION, 'scope': 'local_technical', 'output_sha256': before,
            'duration': duration, 'width': info['width'], 'height': info['height'],
            'decoded_full_output': True, 'audio': audio, 'issues': issues,
            'status': 'needs_attention' if issues else 'checked',
            'not_checked': ['字幕文字准确性', '音画同步与对白清晰度', '故事与综艺观感']}


def run(repo, job_id):
    from ..worker import source_segments
    from .source_review import inspect_source
    with repo.factory() as db:
        job = db.get(Job, job_id)
        version = repo.owned(db, Version, job.version_id, job.owner_id)
        path = validate(repo, db, version)
        if version_digest(version) != job.payload['version_digest']:
            raise DomainError('REVIEW_OUTPUT_CHANGED', '成片已变化，请检查当前版本。', 409)
        expect_audio = bool(version.narration_path) or any(
            db.get(Asset, s['asset_id']).has_audio for s in version.segments)
        source_report = inspect_source(db.get(Episode, version.episode_id),
            source_segments(repo, db, version, subtitle_review=True))
    report = inspect_output(path, expect_audio=expect_audio)
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        version = repo.owned(db, Version, job.version_id, job.owner_id)
        current = validate(repo, db, version)
        if (job.state != 'running' or version_digest(version) != job.payload['version_digest']
                or fingerprint(current) != report['output_sha256']):
            raise DomainError('REVIEW_OUTPUT_CHANGED', '成片已变化，请检查当前版本。', 409)
        job.result = {**report, 'version_id': version.id, 'source_review': source_report}
        job.state, job.stage = 'succeeded', '画面与声音检查完成'
        repo.audit(db, job.owner_id, 'technical_review_completed', job.id)


def context(repo, db, version):
    if version is None or version.state not in ('ready', 'confirmed'):
        return None
    digest = version_digest(version)
    job = db.scalars(select(Job).where(Job.owner_id == version.owner_id,
        Job.idempotency_key == REVISION + ':' + digest, Job.state == 'succeeded')).first()
    if not job or not job.result:
        return None
    path = repo.path(version.output_path)
    if not path.is_file() or fingerprint(path) != job.result.get('output_sha256'):
        return None
    # No file paths or hashes are useful to the speaking director.
    return {k: v for k, v in job.result.items() if k not in ('output_sha256', 'revision')}
