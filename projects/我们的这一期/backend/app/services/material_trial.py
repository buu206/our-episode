"""Shared adapters for the existing three-source consent; no global cloud enablement."""
import hashlib
import json
from pathlib import Path
import re
from sqlalchemy import select
from ..models import Episode, Job
from ..repository import DomainError
from .deepseek import DeepSeekDirector, DeepSeekVision, MODEL as VISION_MODEL
from .director import DirectorError, prepare_input
from .minimax_asr import MiniMaxASR, MODEL as ASR_MODEL
from .pipeline import ModelServices, active_assets
from .speech import SpeechError, _chunk_data

PRODUCT = Path(__file__).resolve().parents[3]

GRANT_FILE = '授权记录/真实素材首测-20261112.json'
ID = re.compile(r'[a-f0-9]{32}')
SHA = re.compile(r'[a-f0-9]{64}')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def signature(chunks):
    return [(c.asset_id, c.offset, c.duration, hashlib.sha256(data).hexdigest())
            for c, data in _chunk_data(chunks)]


def validate_grant(grant):
    def ensure(condition):
        if not condition:
            raise ValueError('grant')
    try:
        ensure(grant['scope'] == 'real-materials-20261112')
        ensure(grant['approval'] == '接受上述范围，仅授权这3段真实素材测试')
        ensure(grant['unknown_vendor_retention_accepted'] is True)
        ensure(grant['authorized_adult_nonsensitive_materials'] is True)
        ensure(grant['owner_id'] == 'local-owner' and ID.fullmatch(grant['episode_id']))
        ensure(grant['task_limit_yuan'] == 3 and grant['total_limit_yuan'] == 30)
        assets = grant['assets']
        ensure(len(assets) == 3 and len({a['id'] for a in assets}) == 3)
        ensure(len({a['sha256'] for a in assets}) == 3)
        for a in assets:
            ensure(ID.fullmatch(a['id']) and SHA.fullmatch(a['sha256']))
            ensure(re.fullmatch(r'assets/[a-f0-9]{32}\.(mp4|mov)', a['path']))
            ensure(type(a['size']) is int and 0 < a['size'] <= 1024 ** 3)
        ensure(sum(a['size'] for a in assets) <= 1024 ** 3)
    except (KeyError, TypeError, ValueError):
        raise DomainError('GRANT_INVALID', '缺少这三段素材的有效授权记录。') from None


def read_grant(root):
    path = root / GRANT_FILE
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 20_000:
        raise DomainError('GRANT_INVALID', '授权记录不存在或路径无效。')
    grant = json.loads(path.read_text())
    validate_grant(grant)
    return grant


def verify_sources(repo, grant):
    validate_grant(grant)
    with repo.factory() as db:
        ep = repo.owned(db, Episode, grant['episode_id'], grant['owner_id'])
        assets = active_assets(db, ep)
        if {a.id for a in assets} != {a['id'] for a in grant['assets']}:
            raise DomainError('SCOPE_CHANGED', '本期素材已变化，本次授权仅覆盖原来的三段。')
        expected = {a['id']: a for a in grant['assets']}
        paths = []
        for a in assets:
            spec = expected[a.id]
            if any(getattr(a, k) != spec[k] for k in ('path', 'size', 'duration', 'width', 'height', 'has_audio')):
                raise DomainError('SCOPE_CHANGED', '素材信息与本次授权不符。')
            path = repo.data_dir / a.path
            if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                    or path.stat().st_size != spec['size']
                    or hashlib.sha256(path.read_bytes()).hexdigest() != spec['sha256']):
                raise DomainError('SCOPE_CHANGED', '素材文件指纹不符，未发送其他内容。')
            paths.append({'asset_id': a.id, 'path': path})
        return paths


def key_guard(key):
    if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
        raise DirectorError('KEY_NOT_CONFIGURED', '测试密钥未配置，不显示配置值。')


class GrantedVision(DeepSeekVision):
    def __init__(self, key, expected, recheck, **kwargs):
        self.expected, self.recheck = expected, recheck
        super().__init__({'DEEPSEEK_API_KEY': key}, **kwargs)

    def _guard(self):
        self.recheck()
        key_guard(self._config.get('DEEPSEEK_API_KEY'))

    async def analyze(self, prepared, *, reservation=None):
        if prepared.request_digest != self.expected:
            raise DirectorError('SCOPE_CHANGED', '抽帧数据不属于本次授权。')
        return await super().analyze(prepared, reservation=reservation)


class GrantedASR(MiniMaxASR):
    def __init__(self, key, expected, recheck, **kwargs):
        self.expected, self.recheck = expected, recheck
        super().__init__({'MINIMAX_API_KEY': key}, **kwargs)

    def _guard(self):
        self.recheck()
        key_guard(self._config.get('MINIMAX_API_KEY'))

    async def transcribe(self, chunks, *, reservation=None):
        if signature(chunks) != self.expected:
            raise SpeechError('SCOPE_CHANGED', '音频数据不属于本次授权。')
        return await super().transcribe(chunks, reservation=reservation)


class GrantedDirector(DeepSeekDirector):
    def __init__(self, key, expected, recheck, **kwargs):
        self.expected, self.recheck = expected, recheck
        super().__init__({'DEEPSEEK_API_KEY': key}, **kwargs)

    def _guard(self):
        self.recheck()
        key_guard(self._config.get('DEEPSEEK_API_KEY'))

    async def propose(self, evidence, assets, background='', *, reservation=None):
        if digest([evidence, assets, background]) != self.expected:
            raise DirectorError('SCOPE_CHANGED', '编导输入与本次确认的内容不符。')
        return await super().propose(evidence, assets, background, reservation=reservation)


class Services(ModelServices):
    def __init__(self, repo, keys, grant):
        super().__init__({'VISION_PROVIDER': 'deepseek', 'ASR_PROVIDER': 'minimax'})
        # Snapshot the grant rather than retaining mutable caller data.
        self.repo, self.keys, self.grant = repo, keys, json.loads(json.dumps(grant))
        self.expected_vision, self.expected_audio = None, None

    def recheck(self):
        return verify_sources(self.repo, self.grant)

    def ready(self):
        try:
            self.recheck()
            for name in ('DEEPSEEK_API_KEY', 'MINIMAX_API_KEY'):
                key_guard(self.keys.get(name))
            return True, '仅本次已授权三段素材；供应商留存期限仍未知。'
        except (DomainError, DirectorError) as exc:
            return False, exc.message

    def profile(self):
        prompt = PRODUCT / 'backend/app/services/prompts/vision.txt'
        return {'scope': self.grant['scope'], 'grant_digest': digest(self.grant),
                'sample_type': 'authorized-real-materials', 'retention_verified': False,
                'unknown_vendor_retention_accepted': True,
                'vision_model': VISION_MODEL, 'asr_model': ASR_MODEL,
                'vision_prompt_sha256': hashlib.sha256(prompt.read_bytes()).hexdigest()}

    def prepare_vision(self, assets, media_root):
        if assets != self.recheck() or media_root != self.repo.data_dir / 'assets':
            raise DomainError('SCOPE_CHANGED', '拒绝读取授权范围以外的画面。')
        prepared = super().prepare_vision(assets, media_root)
        self.expected_vision = prepared.request_digest
        return prepared

    def vision_estimate(self, prepared):
        return DeepSeekVision.estimate(prepared)

    async def vision(self, prepared, reservation, permit):
        return await GrantedVision(self.keys['DEEPSEEK_API_KEY'], self.expected_vision,
            self.recheck, budget_check=permit).analyze(prepared, reservation=reservation)

    def prepare_audio(self, asset, path, directory):
        if {'asset_id': asset.id, 'path': path} not in self.recheck():
            raise DomainError('SCOPE_CHANGED', '拒绝读取授权范围以外的音频。')
        chunks = super().prepare_audio(asset, path, directory)
        self.expected_audio = signature(chunks)
        return chunks

    async def asr(self, chunks, reservation, permit):
        return await GrantedASR(self.keys['MINIMAX_API_KEY'], self.expected_audio,
            self.recheck, budget_check=permit).transcribe(chunks, reservation=reservation)


def confirmed_input(repo, grant):
    verify_sources(repo, grant)
    with repo.factory() as db:
        ep = repo.owned(db, Episode, grant['episode_id'], grant['owner_id'])
        if (not ep.evidence or ep.analysis_state != 'confirmed'
                or ep.evidence_confirmed_revision != ep.evidence_revision):
            raise DomainError('REVIEW_REQUIRED', '请先由用户确认本期内容概览，不自动代替确认。')
        analysis = db.scalars(select(Job).where(Job.owner_id == ep.owner_id, Job.episode_id == ep.id,
                    Job.idempotency_key == grant['scope'] + ':analysis')).first()
        if (analysis is None or analysis.state != 'succeeded'
                or analysis.payload.get('model_profile', {}).get('grant_digest') != digest(grant)
                or set(ep.analysis_asset_ids) != {a['id'] for a in grant['assets']}):
            raise DomainError('SCOPE_CHANGED', '当前内容不属于这三段素材的已完成分析。')
        evidence = [{key: row[key] for key in ('id', 'kind', 'asset_id', 'start', 'end', 'note') if key in row} for row in ep.evidence]
        assets = {a.id: a.duration for a in active_assets(db, ep)}
        from .director_guidance import compiled_background
        background = compiled_background(db, ep)
        prepare_input(evidence, assets, background)
        return ep.evidence_revision, [evidence, assets, background]


class DirectionServices(Services):
    """Freeze the text request; keep the existing paid analysis profile unchanged."""
    def __init__(self, repo, keys, grant):
        super().__init__(repo, keys, grant)
        self.revision, payload = confirmed_input(repo, self.grant)
        self.input_digest = digest(payload)
        self.prompt = PRODUCT / 'backend/app/services/prompts/director.txt'
        self.prompt_digest = hashlib.sha256(self.prompt.read_bytes()).hexdigest()

    def recheck(self):
        revision, payload = confirmed_input(self.repo, self.grant)
        if (revision != self.revision or digest(payload) != self.input_digest
                or hashlib.sha256(self.prompt.read_bytes()).hexdigest() != self.prompt_digest):
            raise DomainError('SCOPE_CHANGED', '内容或编导配置已变化，停止本次请求。')

    def ready(self):
        try:
            self.recheck()
            key_guard(self.keys.get('DEEPSEEK_API_KEY'))
            return True, '仅发送本期已确认的编导文字与时间码。'
        except (DomainError, DirectorError) as exc:
            return False, exc.message

    def profile(self):
        return {'scope': self.grant['scope'], 'grant_digest': digest(self.grant), 'phase': 'directions',
                'sample_type': 'authorized-real-materials', 'retention_verified': False,
                'unknown_vendor_retention_accepted': True, 'director_model': VISION_MODEL,
                'director_prompt_sha256': self.prompt_digest, 'input_digest': self.input_digest,
                'revision': self.revision}

    def director_estimate(self):
        return DeepSeekDirector.required_reservation_yuan

    async def directions(self, evidence, assets, background, reservation, permit):
        return await GrantedDirector(self.keys['DEEPSEEK_API_KEY'], self.input_digest,
            self.recheck, budget_check=permit).propose(evidence, assets, background, reservation=reservation)

