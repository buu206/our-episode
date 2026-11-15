"""Scope the existing three-material consent to explicit director requests.

This does not enable a provider globally or declare its retention verified. The
private consent, ownership, source fingerprints and persisted budget are checked
again immediately before the existing DeepSeek transport sends a request.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import select

from ..models import Budget, Episode, Job, Version
from ..repository import DomainError
from .deepseek import BASE, MODEL, DeepSeekDirector, empty_usage
from .director import DirectorError, _invalid_constant, _unique_object, prepare_input
from .director_chat_model import DeepSeekDirectorChat, prepare_chat_input
from . import director_guidance

GRANT_FILE = '授权记录/真实素材首测-20261112.json'
_ID = re.compile(r'[a-f0-9]{32}')
_SHA = re.compile(r'[a-f0-9]{64}')


def _digest(value):
    # Matches the existing grant digest recorded by authorized_materials.py.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def _stop(code='SCOPE_CHANGED', message='本期内容与原三段素材授权不符，尚未发送请求。'):
    raise DirectorError(code, message, usage=empty_usage())


def _ensure(condition):
    if not condition:
        raise ValueError('invalid grant')


def _read_grant(root):
    path = root / GRANT_FILE
    try:
        if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                or path.stat().st_size > 20_000):
            raise ValueError('grant path')
        grant = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
        _ensure(isinstance(grant, dict))
        _ensure(grant['scope'] == 'real-materials-20261112')
        _ensure(grant['approval'] == '接受上述范围，仅授权这3段真实素材测试')
        _ensure(grant['unknown_vendor_retention_accepted'] is True)
        _ensure(grant['authorized_adult_nonsensitive_materials'] is True)
        _ensure(grant['owner_id'] == 'local-owner' and _ID.fullmatch(grant['episode_id']))
        _ensure(type(grant['task_limit_yuan']) is int and grant['task_limit_yuan'] == 3)
        _ensure(type(grant['total_limit_yuan']) is int and grant['total_limit_yuan'] == 30)
        assets = grant['assets']
        _ensure(isinstance(assets, list) and len(assets) == 3)
        _ensure(len({a['id'] for a in assets}) == len({a['sha256'] for a in assets}) == 3)
        for a in assets:
            _ensure(_ID.fullmatch(a['id']) and _SHA.fullmatch(a['sha256']))
            _ensure(re.fullmatch(r'assets/[a-f0-9]{32}\.(mp4|mov)', a['path']))
            _ensure(type(a['size']) is int and 0 < a['size'] <= 1024 ** 3)
        _ensure(sum(a['size'] for a in assets) <= 1024 ** 3)
        return grant
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        _stop('GRANT_INVALID', '缺少这三段素材的有效授权记录，尚未发送请求。')


def _file_digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


class _ScopedAccess:
    def __init__(self, config, repo, context, permit):
        super().__init__(config, budget_check=permit)
        self._repo = repo
        self._context = deepcopy(context)
        self._grant_digest = None
        self._allowed_messages = None
        self._budget_checks = 0

    def _guard(self):
        if self._config.get('DIRECTOR_PROVIDER', 'bailian') not in ('deepseek', 'bailian'):
            _stop('PROVIDER_NOT_ALLOWED', '导演供应商配置不受支持，未切换服务。')
        key = self._config.get('DEEPSEEK_API_KEY')
        if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
            _stop('KEY_NOT_CONFIGURED', 'DeepSeek 服务端密钥未配置。')
        if self._config.get('DEEPSEEK_BASE_URL', BASE) != BASE:
            _stop('ENDPOINT_NOT_ALLOWED', 'DeepSeek 地址不在白名单中。')
        if self._config.get('DEEPSEEK_MODEL', MODEL) != MODEL:
            _stop('MODEL_NOT_ALLOWED', 'DeepSeek 型号不在本轮验证范围内。')
        self._scope(full=False)

    def ready(self):
        try:
            self._guard()
            return True, '仅本期已授权三段素材可与导演对话；供应商留存期限仍未知。'
        except (DomainError, DirectorError) as exc:
            return False, exc.message

    def _scope(self, *, full):
        try:
            if self._repo is None or not isinstance(self._context, dict):
                _stop('GRANT_REQUIRED', '当前服务未启用，且没有本期范围内的授权。')
            grant = _read_grant(self._repo.data_dir)
            if self._context.get('episode', {}).get('id') != grant['episode_id']:
                _stop()
            checksum = _digest(grant)
            if self._grant_digest is not None and checksum != self._grant_digest:
                _stop('GRANT_CHANGED', '素材授权已变化，尚未发送请求。')
            self._grant_digest = checksum
            from .pipeline import active_assets
            with self._repo.factory() as db:
                ep = self._repo.owned(db, Episode, grant['episode_id'], grant['owner_id'])
                assets = active_assets(db, ep)
                specs = {a['id']: a for a in grant['assets']}
                if {a.id for a in assets} != set(specs):
                    _stop()
                for asset in assets:
                    spec = specs[asset.id]
                    if any(getattr(asset, k) != spec[k] for k in
                           ('path', 'size', 'duration', 'width', 'height', 'has_audio')):
                        _stop()
                    path = self._repo.data_dir / asset.path
                    if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                            or path.stat().st_size != spec['size']):
                        _stop()
                    if full and _file_digest(path) != spec['sha256']:
                        _stop('SCOPE_CHANGED', '素材文件指纹不符，尚未发送请求。')
                analysis = db.scalars(select(Job).where(Job.owner_id == ep.owner_id,
                    Job.episode_id == ep.id, Job.idempotency_key == grant['scope'] + ':analysis')).first()
                profile = analysis.payload.get('model_profile', {}) if analysis is not None else {}
                if (analysis is None or analysis.state != 'succeeded'
                        or profile.get('grant_digest') != checksum
                        or profile.get('scope') != grant['scope']
                        or profile.get('sample_type') != 'authorized-real-materials'
                        or profile.get('vision_model') != MODEL
                        or profile.get('unknown_vendor_retention_accepted') is not True
                        or set(ep.analysis_asset_ids) != set(specs)):
                    _stop('SCOPE_CHANGED', '当前内容不属于原三段素材的已完成分析。')
                return grant
        except DomainError as exc:
            raise DirectorError(exc.code, exc.message, usage=empty_usage()) from None
        except (OSError, KeyError, TypeError, ValueError, AttributeError):
            _stop()

    async def _check_budget(self, reservation, usage):
        await super()._check_budget(reservation, usage)
        self._budget_checks += 1
        # The inherited transport checks twice; the second check is immediately
        # before POST, after any asynchronous client/permit preparation.
        grant = self._scope(full=self._budget_checks >= 2)
        self._validate_payload()
        with self._repo.factory() as db:
            row = db.get(Budget, reservation) if isinstance(reservation, str) else None
            budget = self._repo.budget(db, grant['episode_id'])
            if (row is None or row.owner_id != grant['owner_id'] or row.episode_id != grant['episode_id']
                    or row.state != 'reserved' or row.amount < round(self.required_reservation_yuan * 1e6)
                    or budget['spent'] + budget['reserved'] > 3 + 1e-9
                    or budget['total_spent'] + budget['total_reserved'] > 30 + 1e-9):
                _stop('BUDGET_PERMISSION_REQUIRED', '本期费用预留无效或已达上限，尚未发送请求。')

    async def request(self, messages, reservation, input_limit=None):
        if self._allowed_messages is None or _digest(messages) != self._allowed_messages:
            _stop('SCOPE_CHANGED', '导演请求与已核对的本期文字不符。')
        self._budget_checks = 0
        try:
            return await super().request(messages, reservation, input_limit=input_limit)
        finally:
            self._allowed_messages = None


class ScopedDirectorChat(_ScopedAccess, DeepSeekDirectorChat):
    def _validate_payload(self):
        context = self._context
        from .pipeline import active_assets
        with self._repo.factory() as db:
            ep = self._repo.owned(db, Episode, context['episode']['id'], 'local-owner')
            guidance = director_guidance.state(db, ep)
            expected_episode = {'id': ep.id, 'title': ep.title, 'background': ep.background,
                'analysis_state': ep.analysis_state, 'evidence_revision': ep.evidence_revision,
                'brief_revision': guidance['revision'], 'focus': guidance['focus'],
                'requirements': guidance['notes']}
            expected_assets = [{'id': a.id, 'duration': a.duration, 'has_audio': a.has_audio}
                               for a in active_assets(db, ep)]
            confirmed = ep.analysis_state == 'confirmed' and ep.evidence_confirmed_revision == ep.evidence_revision
            if (context.get('episode') != expected_episode or context.get('assets') != expected_assets
                    or context.get('confirmed_evidence') != (ep.evidence if confirmed else [])):
                _stop('DIRECTOR_CONTEXT_CHANGED', '本期内容或需求已更新，请重新发送导演消息。')
            current = context.get('current_version')
            if current is not None:
                if not isinstance(current, dict) or not isinstance(current.get('id'), str):
                    _stop()
                version = self._repo.owned(db, Version, current['id'], ep.owner_id)
                if version.episode_id != ep.id or version.state == 'restricted':
                    _stop()
                from ..worker import source_segments
                source_segments(self._repo, db, version)
                expected = {k: getattr(version, k) for k in
                            ('id', 'title', 'state', 'segments', 'narration_text', 'packaging')}
                if current != expected or any(s['asset_id'] not in {a['id'] for a in expected_assets}
                                              for s in version.segments):
                    _stop()

    async def chat(self, context, *, reservation=None):
        if _digest(context) != _digest(self._context):
            _stop()
        self._guard()
        self._validate_payload()
        user, _ = prepare_chat_input(context)
        prompt = Path(__file__).with_name('prompts').joinpath('director_chat.txt').read_text(encoding='utf-8')
        self._allowed_messages = _digest([{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user}])
        return await super().chat(context, reservation=reservation)


class ScopedDirector(_ScopedAccess, DeepSeekDirector):
    def ready(self):
        allowed, reason = super().ready()
        if not allowed:
            return allowed, reason
        try:
            self._current_payload()
            return True, '仅本期已确认文字可生成导演方向；仍需已授权的费用预留。'
        except (DomainError, DirectorError) as exc:
            return False, exc.message

    def _current_payload(self):
        from .pipeline import active_assets
        with self._repo.factory() as db:
            ep = self._repo.owned(db, Episode, self._context['episode']['id'], 'local-owner')
            if not ep.evidence or ep.analysis_state != 'confirmed' or ep.evidence_confirmed_revision != ep.evidence_revision:
                _stop('REVIEW_REQUIRED', '请先确认本期内容概览。')
            evidence = [{k: row[k] for k in ('id', 'kind', 'asset_id', 'start', 'end', 'note') if k in row}
                        for row in ep.evidence]
            assets = {a.id: a.duration for a in active_assets(db, ep)}
            background = director_guidance.compiled_background(db, ep)
            return [evidence, assets, background]

    def _validate_payload(self):
        if _digest(self._current_payload()) != self._payload_digest:
            _stop('DIRECTOR_CONTEXT_CHANGED', '本期已确认内容或导演要求变化，尚未发送请求。')

    async def propose(self, evidence, assets, background='', *, reservation=None):
        self._guard()
        self._payload_digest = _digest([evidence, assets, background])
        self._validate_payload()
        user = prepare_input(evidence, assets, background)
        prompt = Path(__file__).with_name('prompts').joinpath('director.txt').read_text()
        self._allowed_messages = _digest([{'role': 'system', 'content': prompt}, {'role': 'user', 'content': user}])
        return await super().propose(evidence, assets, background, reservation=reservation)


def chat_adapter(config, repo=None, context=None, permit=None):
    normal = DeepSeekDirectorChat(config, budget_check=permit)
    if normal.ready()[0] or repo is None or context is None:
        return normal
    return ScopedDirectorChat(config, repo, context, permit)


def directions_adapter(config, repo=None, context=None, permit=None):
    from .provider_config import adapter
    normal = adapter(config, 'director', budget_check=permit)
    if normal.ready()[0] or repo is None or context is None:
        return normal
    return ScopedDirector(config, repo, context, permit)
