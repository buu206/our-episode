#!/usr/bin/env python3
"""One version-bound, human-confirmed narration for the approved three clips."""
import argparse
import hashlib
import json
from pathlib import Path

from authorized_materials import (PRODUCT, ID, SHA, digest, read_grant,
    confirmed_input, key_guard, locked, read_configuration, verify_sources)
from sqlalchemy import select
from app.models import Budget, Episode, Job, ModelCall, Version
from app.repository import DomainError, Repository
from app.services.director import DirectorError
from app.services.minimax_speech import MiniMaxSpeech, MODEL, MAX_AUDIO_BYTES, tts_estimate
from app.services.pipeline import Pipeline
from app.services.speech import SpeechError
from app.worker import source_segments

APPROVAL_FILE = '授权记录/真实素材旁白确认-20261112.json'
VOICE_CONFIG = {'MINIMAX_TTS_MODEL': MODEL, 'MINIMAX_TTS_VOICE': 'male-qn-daxuesheng',
                'MINIMAX_TTS_SPEED': '1.15', 'MINIMAX_TTS_EMOTION': 'happy', 'MINIMAX_TTS_PITCH': '0'}


def version_digest(version):
    return digest({k: getattr(version, k) for k in ('id', 'owner_id', 'episode_id', 'title',
        'origin', 'base_version_id', 'segments', 'evidence_revision', 'direction_id', 'packaging', 'narration_text')})


def validate_approval(grant, approval):
    try:
        valid = (approval['scope'] == grant['scope'] and approval['approval'] == '旁白已确认'
                 and approval['grant_digest'] == digest(grant) and ID.fullmatch(approval['version_id'])
                 and type(approval['revision']) is int and approval['revision'] > 0
                 and all(SHA.fullmatch(approval[k]) for k in ('text_sha256', 'version_digest', 'evidence_digest'))
                 and approval['voice'] == VOICE_CONFIG)
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise DomainError('NARRATION_REVIEW_REQUIRED', '缺少绑定具体版本、文案和音色的人工确认记录。')


def read_approval(root, grant):
    path = root / APPROVAL_FILE
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 20_000:
        raise DomainError('NARRATION_REVIEW_REQUIRED', '旁白确认记录不存在或路径无效。')
    approval = json.loads(path.read_text())
    validate_approval(grant, approval)
    return approval


class GrantedSpeech(MiniMaxSpeech):
    def __init__(self, key, expected_text, output, recheck, **kwargs):
        self.expected_text, self.output, self.recheck = expected_text, Path(output), recheck
        super().__init__({**VOICE_CONFIG, 'MINIMAX_API_KEY': key}, **kwargs)

    def _guard(self):
        try:
            self.recheck()
            key_guard(self._config.get('MINIMAX_API_KEY'))
        except (DomainError, DirectorError) as exc:
            raise SpeechError(exc.code, exc.message) from None
        if any(self._config.get(k) != v for k, v in VOICE_CONFIG.items()):
            raise SpeechError('SCOPE_CHANGED', '音色与已确认设置不符。')
        self._voice()

    async def synthesize(self, text, output_path, *, reservation=None):
        if hashlib.sha256(text.encode()).hexdigest() != self.expected_text or Path(output_path) != self.output:
            raise SpeechError('SCOPE_CHANGED', '旁白文案或保存位置与本次确认不符。')
        result = await super().synthesize(text, output_path, reservation=reservation)
        result['result']['sha256'] = hashlib.sha256(self.output.read_bytes()).hexdigest()
        return result


class NarrationServices:
    is_real = True

    def __init__(self, repo, keys, grant, approval):
        self.repo, self.keys = repo, keys
        self.grant, self.approval = json.loads(json.dumps(grant)), json.loads(json.dumps(approval))
        validate_approval(self.grant, self.approval)

    def recheck(self):
        revision, payload = confirmed_input(self.repo, self.grant)
        if revision != self.approval['revision'] or digest(payload) != self.approval['evidence_digest']:
            raise DomainError('SCOPE_CHANGED', '素材内容已变化，停止本次旁白。')
        with self.repo.factory() as db:
            v = self.repo.owned(db, Version, self.approval['version_id'], self.grant['owner_id'])
            ep = self.repo.owned(db, Episode, self.grant['episode_id'], self.grant['owner_id'])
            if (v.episode_id != ep.id or v.evidence_revision != revision or v.direction_id != ep.selected_direction
                    or v.packaging != 'standard' or v.origin != 'ai' or v.state == 'restricted'
                    or version_digest(v) != self.approval['version_digest']
                    or hashlib.sha256(v.narration_text.encode()).hexdigest() != self.approval['text_sha256']):
                raise DomainError('SCOPE_CHANGED', '版本或旁白与人工确认记录不符。')
            source_segments(self.repo, db, v)
            tts_estimate(v.narration_text)
            return v.narration_text

    def ready(self):
        try:
            self.recheck()
            key_guard(self.keys.get('MINIMAX_API_KEY'))
            return True, '仅本次已确认版本的旁白；供应商留存条件仍未知。'
        except (DomainError, DirectorError, SpeechError) as exc:
            return False, exc.message

    def profile(self):
        return {'scope': self.grant['scope'], 'grant_digest': digest(self.grant),
                'phase': 'narration', 'approval_digest': digest(self.approval),
                'provider': 'minimax', 'model': MODEL, 'voice': dict(VOICE_CONFIG),
                'sample_type': 'authorized-real-materials', 'retention_verified': False,
                'unknown_vendor_retention_accepted': True}

    def tts_estimate(self, text):
        return tts_estimate(text)

    async def tts(self, text, output, reservation, permit):
        if (Path(output).parent != self.repo.data_dir / 'outputs' or Path(output).is_symlink()
                or not ID.fullmatch(Path(output).stem) or Path(output).suffix != '.wav'
                or Path(output).parent.is_symlink()):
            raise SpeechError('SCOPE_CHANGED', '旁白保存位置不在本期受控产物目录。')
        return await GrantedSpeech(self.keys['MINIMAX_API_KEY'], self.approval['text_sha256'],
            output, self.recheck, budget_check=permit).synthesize(text, output, reservation=reservation)


class NarrationPipeline(Pipeline):
    def finish(self, db, job, result, label):
        self.services.recheck()
        super().finish(db, job, result, label)


class FrozenAuditionServices(NarrationServices):
    """Retest the exact previously approved sentence, without applying it to a new edit.

    A changed director brief is irrelevant to a frozen audio audition. It still
    blocks normal narration generation in NarrationServices. Never use this
    class for publishing audio onto a Version.
    """
    def recheck(self):
        verify_sources(self.repo, self.grant)
        with self.repo.factory() as db:
            v = self.repo.owned(db, Version, self.approval['version_id'], self.grant['owner_id'])
            if (v.episode_id != self.grant['episode_id'] or not v.narration_approved
                    or v.state == 'restricted' or version_digest(v) != self.approval['version_digest']
                    or hashlib.sha256(v.narration_text.encode()).hexdigest() != self.approval['text_sha256']):
                raise DomainError('SCOPE_CHANGED', '试听仅允许原已批准版本的同一句文案，不接受新文案或新成片。')
            source_segments(self.repo, db, v)
            tts_estimate(v.narration_text)
            return v.narration_text

    def profile(self):
        return {**super().profile(), 'phase': 'frozen-narration-validation'}


def check_audio(repo, checkpoint):
    path = Path(checkpoint.get('path', ''))
    if (not path.is_absolute() or path.parent != repo.data_dir / 'outputs'
            or path.is_symlink() or path.parent.is_symlink() or not path.is_file()
            or not ID.fullmatch(path.stem) or path.suffix != '.wav'
            or not 0 < path.stat().st_size <= MAX_AUDIO_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != checkpoint.get('sha256')):
        raise DomainError('NARRATION_MISSING', '已付费旁白文件缺失或变化，停止重发并保留费用记录。')


def report(repo, grant, approval, *, validation_only=False):
    with repo.factory() as db:
        v = repo.owned(db, Version, approval['version_id'], grant['owner_id'])
        job = db.scalars(select(Job).where(Job.owner_id == v.owner_id,
            Job.idempotency_key == grant['scope'] + (':audit-narration:' if validation_only else ':narration:') + v.id)).first()
        calls = []
        if job:
            for c in db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)):
                b = db.get(Budget, c.reservation_id)
                calls.append({'id': c.id, 'state': c.state, 'elapsed_seconds': c.elapsed, 'usage': c.usage,
                              'ledger_state': b.state, 'cost_yuan': b.cost / 1e6 if b.cost is not None else None})
        return {'version_id': v.id, 'narration_approved': v.narration_approved,
                'validation_only': validation_only,
                'narration_ready': bool((job.result or {}).get('narration_ready')) if validation_only and job else bool(v.narration_path),
                'audio_path': (job.result or {}).get('audio_path') if validation_only and job else v.narration_path,
                'job_id': job.id if job else None, 'state': job.state if job else None,
                'calls': calls, 'budget': repo.budget(db, v.episode_id)}


def narrate(repo, keys, grant, approval, *, validation_only=False):
    services = (FrozenAuditionServices if validation_only else NarrationServices)(repo, keys, grant, approval)
    pipeline = NarrationPipeline(repo, services)
    pipeline.require_ready()
    profile = services.profile()
    task_key = grant['scope'] + (':audit-narration:' if validation_only else ':narration:') + approval['version_id']
    with repo.transaction() as db:
        v = repo.owned(db, Version, approval['version_id'], grant['owner_id'])
        job = db.scalars(select(Job).where(Job.owner_id == v.owner_id,
            Job.idempotency_key == task_key)).first()
        if job:
            if job.payload.get('model_profile') != profile:
                raise DomainError('SCOPE_CHANGED', '已付费旁白的确认或配置变化，不重复调用。')
            calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)))
            cached = job.payload.get('steps', {}).get('tts')
            settled = all(c.state == 'succeeded' and db.get(Budget, c.reservation_id).state == 'settled'
                          and db.get(Budget, c.reservation_id).cost is not None for c in calls)
            if calls and (not cached or not settled):
                raise DomainError('TRIAL_NEEDS_REVIEW', '已有未完成旁白调用，先核对原因和费用；不会自动重发。')
            if cached:
                check_audio(repo, cached)
            if job.state == 'succeeded':
                expected_path = (job.result or {}).get('audio_path') if validation_only else v.narration_path
                if not cached or expected_path != str(Path(cached['path']).relative_to(repo.data_dir)):
                    raise DomainError('NARRATION_MISSING', '已完成任务的旁白引用不一致，停止重发。')
                return report(repo, grant, approval, validation_only=validation_only)
        elif not validation_only and (v.narration_path or v.state not in ('draft', 'failed')):
            raise DomainError('VERSION_UNAVAILABLE', '此版本已有产物或正在处理，不覆盖生成。')
        active = list(db.scalars(select(Job).where(Job.episode_id == v.episode_id, Job.state.in_(('queued', 'running')))))
        if any(job is None or other.id != job.id for other in active):
            raise DomainError('TASK_RUNNING', '本期已有任务，待其结束后再生成旁白。')
        if job is None:
            job = Job(owner_id=v.owner_id, episode_id=v.episode_id, version_id=v.id, kind='narration', state='running',
                idempotency_key=task_key, payload={
                    'asset_ids': [a['id'] for a in grant['assets']], 'revision': approval['revision'],
                    **({'validation_only': True} if validation_only else {}),
                    'model_profile': profile, 'steps': {}})
            db.add(job); db.flush()
            repo.audit(db, v.owner_id, 'frozen_narration_revalidation_started' if validation_only else 'real_material_narration_confirmed', v.id,
                       confirmation_source='explicit_user_conversation', approval_digest=digest(approval),
                       text_sha256=approval['text_sha256'], voice=VOICE_CONFIG,
                       retention_verified=False, unknown_vendor_retention_accepted=True)
        if not validation_only:
            v.narration_approved = True
            v.job_id, v.state, v.error = job.id, 'voicing', None
        job.state, job.error = 'running', None
        jid = job.id
    try:
        pipeline.narrate(jid)
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            error = {'code': exc.code if isinstance(exc, (DomainError, DirectorError, SpeechError)) else 'TRIAL_FAILED',
                     'message': exc.message if isinstance(exc, (DomainError, DirectorError, SpeechError)) else '旁白未完成，请核对费用与原因。'}
            job.state, job.stage, job.error = 'failed', '真实旁白未完成', error
            v = db.get(Version, job.version_id)
            if not validation_only and v.state == 'voicing':
                v.state, v.error = 'failed', error
        raise
    return report(repo, grant, approval, validation_only=validation_only)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'narrate', 'audit-narration', 'report'))
    args = parser.parse_args(argv)
    repo = None
    try:
        keys = read_configuration(PRODUCT / 'backend/.env')
        root = Path(keys.get('APP_DATA_DIR') or PRODUCT / 'data').resolve()
        if root != (PRODUCT / 'data').resolve() or not (root / 'app.sqlite3').is_file():
            raise DomainError('DATA_DIR_REJECTED', '仅使用已有正常测试数据库。')
        repo = Repository(root)
        grant = read_grant(root)
        approval = read_approval(root, grant)
        with locked(repo):
            if args.action == 'narrate':
                result = narrate(repo, keys, grant, approval)
            elif args.action == 'audit-narration':
                result = narrate(repo, keys, grant, approval, validation_only=True)
            elif args.action == 'check':
                services = NarrationServices(repo, keys, grant, approval)
                ready, reason = services.ready()
                result = {'ready': ready, 'reason': reason, 'network_requests': 0,
                          'reservation_yuan': services.tts_estimate(services.recheck())}
            else:
                result = report(repo, grant, approval)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get('ready', True) else 2
    except (DomainError, DirectorError, SpeechError) as exc:
        print(exc.code + '：' + exc.message)
        return 2
    except (Exception, KeyboardInterrupt):
        print('旁白验证未完成；已发请求不自动重试，费用预留继续保留。')
        return 2
    finally:
        if repo:
            repo.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
