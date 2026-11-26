"""Bounded review of actual output snapshots under the matching episode grant.

No audio is sent, no edit is executed and no version is confirmed. All paid work
uses Pipeline's durable ledger; files and permissions are checked again at send
and publication. A snapshot review is deliberately not a full-film verdict.
"""
import base64
import hashlib
import json
import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select

from ..models import Episode, Job, Version
from ..repository import DomainError
from . import media, render_review
from .deepseek import BASE, MODEL, DeepSeekDirector, empty_usage
from .director import DirectorError, _invalid_constant, _unique_object
from .material_trial import digest, key_guard
from .vision import _jpeg_frame, _check_jpeg

REVISION = 'visual-review-v1'
PROMPT = Path(__file__).with_name('prompts') / 'visual_review.txt'
MAX_FRAMES = 12
RESERVATION = .10


class Finding(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    frame_id: str = Field(min_length=1, max_length=8)
    category: Literal['text', 'occlusion', 'composition', 'effect']
    observation: str = Field(min_length=1, max_length=120)
    suggestion: str = Field(min_length=1, max_length=120)

    @field_validator('observation', 'suggestion')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank finding')
        return value.strip()


class Review(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    summary: str = Field(min_length=1, max_length=120)
    findings: list[Finding] = Field(max_length=3)

    @field_validator('summary')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank summary')
        return value.strip()


def parse(raw, frames):
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        result = Review.model_validate(value)
        valid = {f['frame_id'] for f in frames}
        if any(f.frame_id not in valid for f in result.findings):
            raise ValueError('unobserved frame')
        return result.model_dump()
    except (ValueError, TypeError, ValidationError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '审片结果未通过位置与格式校验。') from None


def snapshot(repo, db, version, *, operation='visual_review'):
    path = render_review.validate(repo, db, version)
    if (path.parent != repo.data_dir / 'outputs' or path.is_symlink()
            or path.parent.is_symlink() or path.suffix != '.mp4'):
        raise DomainError('INVALID_LOCAL_ASSET', '只检查应用内生成的成片。', 409)
    from .review_grant import for_version
    grant = for_version(repo, version, operation)
    if not version.segments or any(s['asset_id'] not in {a['id'] for a in grant['assets']} for s in version.segments):
        raise DomainError('SCOPE_CHANGED', '成片引用了本次授权之外的素材。', 409)
    ep = repo.owned(db, Episode, version.episode_id, version.owner_id)
    return {'version_digest': render_review.version_digest(version),
            'output_sha256': render_review.fingerprint(path), 'grant_digest': digest(grant),
            'prompt_sha256': render_review.fingerprint(PROMPT),
            'asset_ids': sorted(a['id'] for a in grant['assets']), 'revision': ep.evidence_revision}


def validate_job(repo, db, job):
    version = repo.owned(db, Version, job.version_id, job.owner_id)
    if snapshot(repo, db, version) != job.payload['snapshot']:
        raise DomainError('REVIEW_OUTPUT_CHANGED', '成片、来源或审片规则已变化，本次审片已停止。', 409)
    return repo.owned(db, Episode, job.episode_id, job.owner_id), []


def check_config(config):
    key_guard(config.get('DEEPSEEK_API_KEY'))
    if (config.get('DEEPSEEK_BASE_URL', BASE) != BASE
            or config.get('DEEPSEEK_MODEL', MODEL) != MODEL):
        raise DomainError('MODEL_NOT_ALLOWED', '成片画面检查仅使用当前已适配的DeepSeek配置。', 409)


def enqueue(repo, db, version, services):
    check_config(services.config)
    bound = snapshot(repo, db, version)
    key = REVISION + ':' + digest(bound)
    existing = db.scalars(select(Job).where(Job.owner_id == version.owner_id,
                                           Job.idempotency_key == key)).first()
    # Never blindly retry a paid request, including failed/unknown-cost ones.
    if existing:
        return existing
    job = Job(owner_id=version.owner_id, episode_id=version.episode_id, version_id=version.id,
              kind='visual_review', idempotency_key=key, payload={'snapshot': bound},
              stage='等待导演检查抽样画面')
    db.add(job)
    db.flush()
    return job


def prepare(repo, job_id):
    with repo.factory() as db:
        job = db.get(Job, job_id)
        validate_job(repo, db, job)
        version = db.get(Version, job.version_id)
        path = repo.path(version.output_path)
    duration = media.probe(path)['duration']
    if not 0 < duration <= media.MAX_OUTPUT_SECONDS + .15:
        raise DomainError('REVIEW_DURATION_LIMIT', '成片需在90秒试验范围内。', 409)
    count = min(MAX_FRAMES, max(1, math.ceil(duration / 3)))
    frames = []
    for i in range(count):
        time = round((i + .5) * duration / count, 6)
        jpeg = _jpeg_frame(path, time)
        _check_jpeg(jpeg)
        frames.append({'frame_id': f'f{i + 1}', 'time': time, 'jpeg': jpeg})
    # Mutation during local extraction must not get as far as a supplier.
    with repo.factory() as db:
        validate_job(repo, db, db.get(Job, job_id))
    return duration, frames


class Adapter(DeepSeekDirector):
    input_limit = 40000
    max_output_tokens = 2048
    required_reservation_yuan = RESERVATION

    def __init__(self, config, recheck, **kwargs):
        self.recheck = recheck
        super().__init__(config, **kwargs)

    def _guard(self):
        check_config(self._config)
        self.recheck()

    async def _check_budget(self, reservation, usage):
        await super()._check_budget(reservation, usage)
        self._guard()  # Includes the check immediately before transport POST.

    async def review(self, frames, reservation):
        content = [{'type': 'text', 'text': PROMPT.read_text()}]
        for frame in frames:
            content.extend([{'type': 'text', 'text': json.dumps({'frame_id': frame['frame_id'], 'time': frame['time']})},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' +
                 base64.b64encode(frame['jpeg']).decode(), 'detail': 'original'}}])
        text_bytes = sum(len(c['text'].encode()) for c in content if c['type'] == 'text')
        if text_bytes + 2048 + len(frames) * 1024 > self.input_limit:
            raise DirectorError('INPUT_LIMIT', '审片抽样超过本次费用预留范围。', usage=empty_usage())
        raw, usage = await self.request([{'role': 'user', 'content': content}], reservation)
        try:
            return {'result': parse(raw, frames), 'usage': usage}
        except DirectorError as exc:
            exc.usage = usage
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
                check_config(upstream.config)
                recheck()
                return True, '仅本期授权素材生成的成片抽帧。'
            except (DirectorError, DomainError) as exc:
                return False, exc.message
        def profile(self):
            with repo.factory() as db:
                bound = db.get(Job, job_id).payload['snapshot']
            return {'scope': REVISION, 'model': MODEL, 'snapshot_digest': digest(bound)}
    scoped = Pipeline(repo, ScopedServices())
    duration, frames = prepare(repo, job_id)
    packet_digest = digest([{'frame_id': f['frame_id'], 'time': f['time'],
                            'sha256': hashlib.sha256(f['jpeg']).hexdigest()} for f in frames])
    result = scoped.invoke(job_id, 'visual_review', RESERVATION,
        lambda reservation, permit: Adapter(upstream.config, recheck, budget_check=permit).review(frames, reservation),
        request_digest=packet_digest, checkpoint_key='visual_review')
    if hasattr(result, 'model_dump'):
        result = result.model_dump()
    # Cached paid results still pass today's schema before publication.
    result = parse(json.dumps(result), frames)
    positions = {f['frame_id']: f['time'] for f in frames}
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        validate_job(repo, db, job)
        if job.state != 'running':
            raise DomainError('JOB_INTERRUPTED', '审片任务已停止。', 409)
        report = {**result, 'version_id': job.version_id, 'scope': 'sampled_visual',
                  'verification': 'unverified', 'audio_analyzed': False, 'duration': duration,
                  'output_sha256': job.payload['snapshot']['output_sha256'],
                  'sample_times': list(positions.values()),
                  'findings': [{**f, 'time': positions[f['frame_id']]} for f in result['findings']],
                  'not_checked': ['未采样画面与连续动作', '原声、字幕原话准确性和音画同步', '整片故事与综艺效果']}
        scoped.finish(db, job, report, '导演抽样画面检查完成')


def context(repo, db, version):
    if version is None or version.state not in ('ready', 'confirmed'):
        return None
    job = db.scalars(select(Job).where(Job.owner_id == version.owner_id,
        Job.version_id == version.id, Job.kind == 'visual_review', Job.state == 'succeeded')
        .order_by(Job.created_at.desc())).first()
    if not job or not job.result:
        return None
    try:
        validate_job(repo, db, job)
    except (DomainError, DirectorError, OSError, ValueError):
        return None
    return {k: v for k, v in job.result.items() if k != 'output_sha256'}
