"""Durable, bounded local delegation over existing jobs; no model autonomy implied."""
import hashlib
import json
import time
from sqlalchemy import select
from ..models import Episode, Job, Version
from ..repository import DomainError
from . import director_guidance

LIVE = ('active', 'pausing', 'paused', 'blocked', 'cancelling')
TRANSIENT = ('MEDIA_TIMEOUT', 'INTERRUPTED')


def intent(version):
    return hashlib.sha256(json.dumps({k: getattr(version, k) for k in
        ('segments', 'packaging', 'narration_text', 'evidence_revision', 'direction_id')}, sort_keys=True).encode()).hexdigest()


def find(db, version):
    return db.scalars(select(Job).where(Job.kind == 'director_run', Job.version_id == version.id,
        Job.owner_id == version.owner_id).order_by(Job.created_at.desc())).first()


def _publish(run, child=None):
    run.result = {'version_id': run.version_id, 'phase': run.payload['phase'],
        'child_state': child.state if child else None, 'retries_used': run.payload['retries_used'],
        'retry_limit': 1, 'cloud_calls_allowed': False, 'stop_at': 'preview',
        'expires_at': run.payload['expires_at']}


def create(repo, db, version, render_job, brief_revision):
    run = Job(owner_id=version.owner_id, episode_id=version.episode_id, version_id=version.id,
        kind='director_run', state='active', stage='导演正在安排本机初稿',
        idempotency_key='director-run:' + render_job.id,
        payload={'phase': 'render', 'child_id': render_job.id, 'intent': intent(version),
            'brief_revision': brief_revision, 'retries_used': 0,
            'expires_at': time.time() + 3600, 'cloud_calls_allowed': False, 'stop_at': 'preview'})
    db.add(run); db.flush()
    _publish(run, render_job)
    repo.audit(db, run.owner_id, 'director_run_created', run.id)
    return run


def validate(repo, db, run):
    from ..worker import source_segments
    version = repo.owned(db, Version, run.version_id, run.owner_id)
    episode = repo.owned(db, Episode, run.episode_id, run.owner_id)
    if version.episode_id != episode.id or intent(version) != run.payload['intent']:
        raise DomainError('RUN_INPUT_CHANGED', '这份委托的拍法已变化，请重新选择拍法。', 409)
    if time.time() >= run.payload['expires_at']:
        raise DomainError('RUN_EXPIRED', '这份制作委托已超过一小时，已停止继续派发。', 409)
    director_guidance.assert_revision(db, episode, run.payload['brief_revision'])
    source_segments(repo, db, version)
    child = repo.owned(db, Job, run.payload['child_id'], run.owner_id)
    if (child.version_id != version.id or child.episode_id != episode.id
            or child.kind != run.payload['phase'] or child.kind not in ('render', 'technical_review')):
        raise DomainError('RUN_SCOPE_CHANGED', '委托与制作任务不一致，已停止。', 409)
    return version, child


def _hold(child):
    if child and child.state == 'queued':
        child.state, child.stage = 'paused', '导演委托已暂停'


def control(repo, db, version, action):
    run = find(db, version)
    if not run:
        raise DomainError('NOT_FOUND', '这一版没有可控制的导演委托。', 404)
    child = db.get(Job, run.payload['child_id'])
    if action == 'pause':
        if run.state in ('paused', 'pausing'):
            return run
        if run.state != 'active':
            raise DomainError('RUN_NOT_ACTIVE', '委托当前不在执行。', 409)
        _hold(child)
        run.state = 'pausing' if child and child.state == 'running' else 'paused'
        run.stage = '正在收尾当前合成，之后暂停' if run.state == 'pausing' else '已暂停，不再启动下一步'
    elif action == 'resume':
        if run.state == 'active':
            return run
        if run.state not in ('paused', 'blocked'):
            raise DomainError('RUN_NOT_RESUMABLE', '这份委托不能继续，请查看当前候选。', 409)
        _, child = validate(repo, db, run)
        if child.state == 'failed':
            if child.error and child.error.get('code') in TRANSIENT and run.payload['retries_used'] < 1:
                _retry(db, run, child)
            else:
                raise DomainError('RUN_RETRY_LIMIT', '这一步无法自动重试，已有输入保留，请先核对失败原因。', 409)
        elif child.state == 'paused':
            child.state, child.stage = 'queued', '导演已继续制作'
        run.state, run.stage, run.error = 'active', '导演继续处理这份初稿', None
    elif action == 'cancel':
        if run.state in ('cancelled', 'cancelling'):
            return run
        if run.state not in LIVE:
            raise DomainError('RUN_FINISHED', '委托已交付，候选继续保留。', 409)
        if child and child.state in ('queued', 'paused'):
            child.state, child.stage = 'cancelled', '委托已结束，未继续执行'
            if child.kind == 'render' and version.state == 'rendering':
                version.state, version.job_id = 'draft', None
        run.state = 'cancelling' if child and child.state == 'running' else 'cancelled'
        run.stage = '收尾当前合成后结束，已有结果保留' if run.state == 'cancelling' else '已结束委托，已有结果保留'
    else:
        raise DomainError('INVALID_ACTION', '不支持的委托操作。', 422)
    _publish(run, child)
    repo.audit(db, run.owner_id, 'director_run_' + action, run.id)
    return run


def _retry(db, run, child):
    run.payload = {**run.payload, 'retries_used': run.payload['retries_used'] + 1}
    child.state, child.error, child.stage = 'queued', None, '本机步骤中断，自动再试一次'
    if child.kind == 'render':
        version = db.get(Version, child.version_id)
        version.state, version.error = 'rendering', None


def advance(repo):
    """Called before/after worker units; never dispatches a cloud operation."""
    with repo.factory() as db:
        ids = list(db.scalars(select(Job.id).where(Job.kind == 'director_run',
            Job.state.in_(('active', 'pausing', 'paused', 'cancelling')))))
    for rid in ids:
        with repo.transaction() as db:
            run = db.get(Job, rid)
            if not run or run.state not in LIVE:
                continue
            child = db.get(Job, run.payload['child_id'])
            if run.state == 'cancelling':
                if child and child.state == 'running':
                    continue
                run.state, run.stage = 'cancelled', '已结束委托，已有结果保留'
                _publish(run, child)
                continue
            try:
                version, child = validate(repo, db, run)
            except DomainError as exc:
                _hold(child)
                run.state, run.stage = 'blocked', '委托已停止，需核对来源或拍法'
                run.error = {'code': exc.code, 'message': exc.message}
                _publish(run, child)
                continue
            if run.state in ('pausing', 'paused', 'blocked'):
                _hold(child)
                if run.state == 'pausing' and child.state != 'running':
                    run.state, run.stage = 'paused', '已暂停，完成的结果已保存'
                _publish(run, child)
                continue
            if child.state == 'failed':
                if (child.error or {}).get('code') in TRANSIENT and run.payload['retries_used'] < 1:
                    _retry(db, run, child)
                    repo.audit(db, run.owner_id, 'director_local_retry', run.id, child_id=child.id)
                else:
                    run.state, run.stage, run.error = 'blocked', '这一步没有完成，输入与已有结果已保存', child.error
            elif child.state == 'succeeded':
                if run.payload['phase'] == 'render':
                    from .render_review import enqueue
                    try:
                        child = enqueue(repo, db, version)
                        run.payload = {**run.payload, 'phase': 'technical_review', 'child_id': child.id}
                        run.stage = '初稿已生成，正在检查画面与声音'
                    except DomainError as exc:
                        run.state, run.stage = 'blocked', '后续检查已停止，已生成的版本保留'
                        run.error = {'code': exc.code, 'message': exc.message}
                else:
                    run.state, run.stage = 'awaiting_user', '初稿和播放检查已完成，候选待你预览'
                    repo.audit(db, run.owner_id, 'director_run_delivered', run.id)
            _publish(run, child)


def owns_render(db, version):
    # An ended delegation must also suppress implicit next-step dispatch when a
    # render that was already running completes after cancellation.
    run = find(db, version)
    return bool(run and run.payload['phase'] == 'render' and run.payload['child_id'] == version.job_id)


def require_manual_allowed(db, version):
    run = find(db, version)
    if run and run.state in LIVE:
        raise DomainError('DIRECTOR_RUN_ACTIVE', '导演正在负责这一版；请先继续或结束当前委托。', 409)
