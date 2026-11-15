"""Route an already granted episode through the same guarded production jobs.

No consent is inferred from a key or a click. The private three-file grant,
owner, exact bytes, model bindings and existing ledger are checked again for
paid calls. Other episodes retain normal provider admission.
"""
from copy import deepcopy
from pathlib import Path
from sqlalchemy import select
from ..models import Episode, Job, Version
from ..repository import DomainError
from .director import DirectorError
from .material_trial import (Services, DirectionServices, read_grant, verify_sources,
                             digest, key_guard, GRANT_FILE, confirmed_input)
from .minimax_speech import MiniMaxSpeech, MODEL, tts_estimate
from .speech import SpeechError

VOICE = {'MINIMAX_TTS_MODEL': MODEL, 'MINIMAX_TTS_VOICE': 'male-qn-daxuesheng',
         'MINIMAX_TTS_SPEED': '1.15', 'MINIMAX_TTS_EMOTION': 'happy', 'MINIMAX_TTS_PITCH': '0'}


class LiveGrant:
    def recheck(self):
        if digest(read_grant(self.repo.data_dir)) != digest(self.grant):
            raise DomainError('GRANT_CHANGED', '本期素材授权已变化，任务停止。', 409)
        return super().recheck()


class AnalysisServices(LiveGrant, Services):
    pass


class DirectServices(LiveGrant, DirectionServices):
    pass


class SpeechAdapter(MiniMaxSpeech):
    def __init__(self, config, service, output, **kwargs):
        super().__init__(config, **kwargs)
        self.service, self.output = service, output

    def _guard(self):
        try:
            self.service.recheck(approved=True)
            key_guard(self._config.get('MINIMAX_API_KEY'))
            self._voice()
        except (DomainError, DirectorError) as exc:
            raise SpeechError(exc.code, exc.message) from None

    async def synthesize(self, text, output_path, *, reservation=None):
        if text != self.service.text or output_path != self.output:
            raise SpeechError('SCOPE_CHANGED', '旁白内容与当前版本不符。')
        return await super().synthesize(text, output_path, reservation=reservation)


class NarrationServices:
    is_real = True

    def __init__(self, repo, config, grant, version_id):
        self.repo, self.config, self.grant = repo, config, deepcopy(grant)
        self.version_id = version_id
        with repo.factory() as db:
            v = repo.owned(db, Version, version_id, grant['owner_id'])
            self.text = v.narration_text
            self.version_hash = self.fingerprint(v)
        self.recheck()

    @staticmethod
    def fingerprint(v):
        return digest({k:getattr(v,k) for k in ('id','owner_id','episode_id','segments',
            'narration_text','evidence_revision','packaging')})

    def recheck(self, approved=False):
        if digest(read_grant(self.repo.data_dir)) != digest(self.grant):
            raise DomainError('GRANT_CHANGED', '本期素材授权已变化。', 409)
        confirmed_input(self.repo, self.grant)
        from ..worker import source_segments
        with self.repo.factory() as db:
            v = self.repo.owned(db, Version, self.version_id, self.grant['owner_id'])
            if (v.episode_id != self.grant['episode_id'] or v.state == 'restricted'
                    or self.fingerprint(v) != self.version_hash or (approved and not v.narration_approved)):
                raise DomainError('NARRATION_REVIEW_REQUIRED', '请先确认本版本的具体旁白。', 409)
            source_segments(self.repo, db, v)
        tts_estimate(self.text)
        for key, value in VOICE.items():
            if str(self.config.get(key, value)) != value:
                raise DomainError('VOICE_CHANGED', '音色设置已改变，请重新核对音色。', 409)

    def ready(self):
        try:
            self.recheck()
            key_guard(self.config.get('MINIMAX_API_KEY'))
            return True, '本版旁白可生成；仍需确认具体文案。'
        except (DomainError, DirectorError, SpeechError) as exc:
            return False, exc.message

    def profile(self):
        return {'scope':self.grant['scope'], 'grant_digest':digest(self.grant),
                'phase':'narration-web', 'provider':'minimax', 'model':MODEL, 'voice':VOICE,
                'version_digest':self.version_hash, 'sample_type':'authorized-real-materials',
                'retention_verified':False, 'unknown_vendor_retention_accepted':True}

    def tts_estimate(self, text):
        return tts_estimate(text)

    async def tts(self, text, output, reservation, permit):
        if (Path(output).parent != self.repo.data_dir/'outputs' or Path(output).is_symlink()
                or Path(output).suffix != '.wav'):
            raise SpeechError('INVALID_AUDIO_PATH', '旁白保存位置无效。')
        return await SpeechAdapter({**VOICE,'MINIMAX_API_KEY':self.config.get('MINIMAX_API_KEY')},
            self, output, budget_check=permit).synthesize(text,output,reservation=reservation)


def resolve(base, repo, episode_id, owner_id, kind, version_id=None, *, bound=None):
    """Return (adapter, grant marker). A bound job cannot fall back to global mode."""
    if not base.is_real:
        if bound: raise DomainError('SCOPE_CHANGED','真实授权任务不能切换为演示。',409)
        return base, None
    path=repo.data_dir/GRANT_FILE
    if bound is None and base.ready()[0]:
        return base, None
    if not path.exists() and not path.is_symlink():
        if bound: raise DomainError('GRANT_REQUIRED','本期素材授权已撤回。',409)
        return base, None
    grant=read_grant(repo.data_dir)
    if episode_id != grant['episode_id'] or owner_id != grant['owner_id']:
        if bound: raise DomainError('SCOPE_CHANGED','任务不属于本期授权。',409)
        return base, None
    marker={'scope':grant['scope'],'grant_digest':digest(grant)}
    if bound is not None and bound != marker:
        raise DomainError('GRANT_CHANGED','任务授权已变化，请核对后继续。',409)
    config=getattr(base,'config',{})
    cls={'analysis':AnalysisServices,'directions':DirectServices}.get(kind)
    if cls:
        return cls(repo,config,grant), marker
    if kind=='narration' and version_id:
        return NarrationServices(repo,config,grant,version_id), marker
    raise DomainError('UNSUPPORTED_JOB','不支持该授权任务。',409)


def capability(base,repo,episode_id,owner_id):
    """Only an indication for this episode; execution always rechecks admission."""
    if not base.is_real or base.ready()[0]: return None
    if not (repo.data_dir/GRANT_FILE).exists(): return None
    try:
        service,marker=resolve(base,repo,episode_id,owner_id,'analysis')
        if marker is None:return None
        ready,reason=service.ready()
        if not ready:return {'enabled':False,'reason':reason,'analysis_cached':False}
        key_guard(getattr(base,'config',{}).get('MINIMAX_API_KEY'))
        with repo.factory() as db:
            ep=repo.owned(db,Episode,episode_id,owner_id)
            original=db.scalars(select(Job).where(Job.owner_id==owner_id,
                Job.episode_id==episode_id,Job.idempotency_key==service.grant['scope']+':analysis')).first()
            cached=bool(original and original.state=='succeeded' and ep.evidence and
                        ep.analysis_state in ('review','confirmed') and
                        original.payload.get('model_profile',{}).get('grant_digest')==marker['grant_digest'])
        return {'enabled':True,'reason':'本期三段素材已授权使用DeepSeek和MiniMax；供应商留存期限仍未知。',
                'analysis_cached':cached}
    except (DomainError,DirectorError,SpeechError,ValueError,OSError):
        return {'enabled':False,'reason':'本期授权或模型配置需核对，未发送素材。','analysis_cached':False}
