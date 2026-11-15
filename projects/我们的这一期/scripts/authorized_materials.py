#!/usr/bin/env python3
"""Operator-only analysis and directions for three approved, fingerprinted uploads.

The private grant records acceptance of unknown vendor retention; it never sets
RETENTION_VERIFIED or enables other episodes. Review and selection remain human.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from sqlalchemy import select
from app.models import Asset, Budget, Episode, Job, ModelCall
from app.repository import DomainError, Repository
from app.services.deepseek import DeepSeekDirector, DeepSeekVision, MODEL as VISION_MODEL
from app.services.director import DirectorError, prepare_input
from app.services.minimax_asr import MiniMaxASR, MODEL as ASR_MODEL
from app.services.pipeline import ModelServices, Pipeline, active_assets
from app.services.speech import SpeechError, _chunk_data
from cloud_preflight import read_configuration

from app.services.material_trial import (GRANT_FILE, ID, SHA, digest, signature, validate_grant, read_grant, verify_sources, key_guard, GrantedVision, GrantedASR, GrantedDirector, Services, confirmed_input, DirectionServices)


def report(repo, grant):
    with repo.factory() as db:
        ep = repo.owned(db, Episode, grant['episode_id'], grant['owner_id'])
        jobs = list(db.scalars(select(Job).where(Job.episode_id == ep.id,
                    Job.idempotency_key.in_([grant['scope'] + ':analysis', grant['scope'] + ':directions']))))
        calls = []
        for job in jobs:
            for call in db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)):
                ledger = db.get(Budget, call.reservation_id)
                calls.append({'id': call.id, 'kind': call.kind, 'state': call.state,
                    'elapsed_seconds': call.elapsed, 'usage': call.usage,
                    'ledger_state': ledger.state, 'cost_yuan': ledger.cost / 1e6 if ledger.cost is not None else None})
        return {'url': f'http://127.0.0.1:3000/episodes/{ep.id}?step=direction',
                'analysis_state': ep.analysis_state, 'evidence_count': len(ep.evidence),
                'visual_count': sum(e.get('kind') == 'visual' for e in ep.evidence),
                'speech_count': sum(e.get('kind') == 'speech' for e in ep.evidence),
                'human_confirmed': ep.evidence_confirmed_revision == ep.evidence_revision,
                'direction_count': len(ep.directions), 'directions_revision': ep.directions_revision,
                'evidence_revision': ep.evidence_revision,
                'jobs': [{'id': j.id, 'kind': j.kind, 'state': j.state, 'error': j.error} for j in jobs],
                'calls': calls, 'budget': repo.budget(db, ep.id)}


def analyze(repo, keys, grant, *, resume=False, validation_only=False):
    services = Services(repo, keys, grant)
    pipeline = Pipeline(repo, services)
    pipeline.require_ready()
    profile = services.profile()
    task_key = grant['scope'] + (':analysis-validation-v1' if validation_only else ':analysis')
    with repo.transaction() as db:
        ep = repo.owned(db, Episode, grant['episode_id'], grant['owner_id'])
        job = db.scalars(select(Job).where(Job.owner_id == grant['owner_id'],
                    Job.idempotency_key == task_key)).first()
        if job:
            if job.payload.get('model_profile') != profile:
                raise DomainError('SCOPE_CHANGED', '已付费任务的授权或配置发生变化，不重复调用。')
            if job.state == 'succeeded':
                return validation_report(repo, job.id, grant) if validation_only else report(repo, grant)
            calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)))
            complete = {'vision', *('asr:' + a['id'] for a in grant['assets'])}.issubset(job.payload.get('steps', {}))
            settled = all(c.state == 'succeeded' and db.get(Budget, c.reservation_id).state == 'settled'
                          and db.get(Budget, c.reservation_id).cost is not None for c in calls)
            if calls and (not complete or not settled):
                # At most two explicit operator resumes for known-cost
                # schema failure. Never auto-retry or erase the failed call.
                known = all(db.get(Budget, c.reservation_id).state == 'settled'
                            and db.get(Budget, c.reservation_id).cost is not None for c in calls)
                allowed = (resume and known and job.state == 'failed'
                           and (job.error or {}).get('code') == 'INVALID_ASR_RESULT'
                           and job.payload.get('manual_resume_count', 0) < 2)
                if not allowed:
                    raise DomainError('TRIAL_NEEDS_REVIEW', '已有未完成调用，先核对原因和费用；不会自动重发。')
                job.payload = {**job.payload, 'manual_resume_count': job.payload.get('manual_resume_count', 0) + 1,
                               'previous_failure': job.error}
                repo.audit(db, ep.owner_id, 'real_material_trial_resumed', job.id,
                           reason='diagnosed_known_cost_asr_schema', scope=grant['scope'])
        else:
            if not validation_only and (ep.evidence or ep.analysis_state not in ('empty', 'stale')):
                raise DomainError('REVIEW_EXISTS', '本期已有分析结果，不覆盖人工核对。')
            if db.scalars(select(Job).where(Job.episode_id == ep.id, Job.state.in_(('queued', 'running')))).first():
                raise DomainError('TASK_RUNNING', '本期已有任务，待其结束后再验证。')
            job = Job(owner_id=ep.owner_id, episode_id=ep.id, kind='analysis',
                idempotency_key=task_key, payload={
                    'asset_ids': [a['id'] for a in grant['assets']], 'revision': ep.evidence_revision,
                    **({'validation_only': True} if validation_only else {}),
                    'model_profile': profile, 'steps': {}})
            db.add(job); db.flush()
            repo.audit(db, ep.owner_id, 'real_material_trial_authorized', job.id,
                       scope=grant['scope'], grant_digest=digest(grant), retention_verified=False,
                       unknown_vendor_retention_accepted=True)
        job.state, job.error = 'running', None
        jid = job.id
    try:
        pipeline.analyze(jid)
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '真实素材分析未完成'
            job.error = {'code': exc.code if isinstance(exc, (DomainError, DirectorError)) else 'TRIAL_FAILED',
                         'message': exc.message if isinstance(exc, (DomainError, DirectorError)) else '处理未完成，请核对费用与原因。'}
        raise
    return validation_report(repo, jid, grant) if validation_only else report(repo, grant)


def validation_report(repo, job_id, grant):
    with repo.factory() as db:
        job = repo.owned(db, Job, job_id, grant['owner_id'])
        return {'job_id': job.id, 'state': job.state, 'validation_only': True,
                'evidence_count': (job.result or {}).get('evidence_count', 0),
                'direction_count': (job.result or {}).get('direction_count', 0),
                'budget': repo.budget(db, grant['episode_id'])}


def directions(repo, keys, grant, *, validation_only=False):
    services = DirectionServices(repo, keys, grant)
    pipeline = Pipeline(repo, services)
    pipeline.require_ready()
    profile = services.profile()
    task_key = grant['scope'] + (':directions-validation-v1' if validation_only else ':directions')
    with repo.transaction() as db:
        ep = repo.owned(db, Episode, grant['episode_id'], grant['owner_id'])
        job = db.scalars(select(Job).where(Job.owner_id == ep.owner_id, Job.episode_id == ep.id,
                    Job.idempotency_key == task_key)).first()
        if job:
            if job.payload.get('model_profile') != profile or job.payload.get('revision') != ep.evidence_revision:
                raise DomainError('SCOPE_CHANGED', '已有编导任务的内容或配置已变化，不重复调用。')
            from app.services.director_guidance import assert_revision
            assert_revision(db, ep, job.payload.get('brief_revision', 0))
            if job.state == 'succeeded':
                return validation_report(repo, job.id, grant) if validation_only else report(repo, grant)
            calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)))
            complete = 'directions' in job.payload.get('steps', {})
            settled = all(c.state == 'succeeded' and db.get(Budget, c.reservation_id).state == 'settled'
                          and db.get(Budget, c.reservation_id).cost is not None for c in calls)
            if calls and (not complete or not settled):
                raise DomainError('TRIAL_NEEDS_REVIEW', '已有编导调用未完成，先核对原因和费用；不会自动重发。')
        active = list(db.scalars(select(Job).where(Job.episode_id == ep.id, Job.state.in_(('queued', 'running')))))
        if any(job is None or other.id != job.id for other in active):
            raise DomainError('TASK_RUNNING', '本期已有任务，待其结束后再验证。')
        if job is None:
            from app.services.director_guidance import revision as brief_revision
            job = Job(owner_id=ep.owner_id, episode_id=ep.id, kind='directions', state='running',
                idempotency_key=task_key, payload={
                    'asset_ids': [a['id'] for a in grant['assets']], 'revision': services.revision,
                    **({'validation_only': True} if validation_only else {}),
                    'brief_revision': brief_revision(db, ep), 'model_profile': profile, 'steps': {}})
            db.add(job); db.flush()
            repo.audit(db, ep.owner_id, 'real_material_directions_authorized', job.id,
                       scope=grant['scope'], grant_digest=digest(grant), input_digest=services.input_digest,
                       revision=services.revision, retention_verified=False,
                       unknown_vendor_retention_accepted=True)
        job.state, job.error = 'running', None
        jid = job.id
    try:
        pipeline.directions(jid)
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '真实素材编导未完成'
            job.error = {'code': exc.code if isinstance(exc, (DomainError, DirectorError)) else 'TRIAL_FAILED',
                         'message': exc.message if isinstance(exc, (DomainError, DirectorError)) else '处理未完成，请核对费用与原因。'}
        raise
    return validation_report(repo, jid, grant) if validation_only else report(repo, grant)


@contextmanager
def locked(repo):
    with (repo.data_dir / '.authorized-materials.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError('TASK_RUNNING', '已有授权素材验证运行中。') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'analyze', 'resume', 'audit-analysis', 'directions', 'audit-directions', 'report'))
    args = parser.parse_args(argv)
    repo = None
    try:
        values = read_configuration(PRODUCT / 'backend/.env')
        root = Path(values.get('APP_DATA_DIR') or PRODUCT / 'data').resolve()
        if root != (PRODUCT / 'data').resolve() or not (root / 'app.sqlite3').is_file():
            raise DomainError('DATA_DIR_REJECTED', '仅使用已有正常测试数据库，不创建或切换其他库。')
        grant = read_grant(root)
        repo = Repository(root)
        with locked(repo):
            if args.action in ('analyze', 'resume'):
                result = analyze(repo, values, grant, resume=args.action == 'resume')
            elif args.action == 'audit-analysis':
                result = analyze(repo, values, grant, validation_only=True)
            elif args.action == 'directions':
                result = directions(repo, values, grant)
            elif args.action == 'audit-directions':
                result = directions(repo, values, grant, validation_only=True)
            elif args.action == 'check':
                ready, reason = Services(repo, values, grant).ready()
                result = {'ready': ready, 'reason': reason, 'network_requests': 0}
            else:
                result = report(repo, grant)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('ready', True) else 2
    except (DomainError, DirectorError) as exc:
        print(exc.code + '：' + exc.message)
        return 2
    except (Exception, KeyboardInterrupt):
        print('本次验证未完成；已发请求不自动重试，费用预留继续保留。')
        return 2
    finally:
        if repo:
            repo.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
