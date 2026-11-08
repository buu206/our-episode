"""Persisted, human-reviewed media workflow. Production has no mock mode."""
import asyncio
import hashlib
import json
import math
import tempfile
import time
from sqlalchemy import select
from ..models import Asset, Budget, Episode, Job, ModelCall, Version, uid
from ..repository import DomainError
from .director import CloudDirector, DirectorError, parse_result


def active_assets(db, episode):
    from .collaboration import asset_details, guard_asset
    candidates = list(db.scalars(select(Asset).where(Asset.episode_id == episode.id,
        Asset.owner_id == episode.owner_id, Asset.deleted == False).order_by(Asset.created_at)))
    assets = []
    for asset in candidates:
        if asset_details(db, asset)['source_kind'] == 'interview':
            continue  # After-event statements must not be analysed as original events.
        guard_asset(db, asset)
        assets.append(asset)
    return assets


def assert_sources(db, episode, expected):
    assets = active_assets(db, episode)
    if sorted(a.id for a in assets) != sorted(expected):
        raise DomainError('SOURCES_CHANGED', '素材已变化，请重新分析并核对证据。', 409)
    return assets


def invalidate_analysis(repo, db, episode):
    from .sharing import suspend_version_shares
    episode.evidence_revision += 1
    episode.evidence_confirmed_revision = None
    episode.directions_revision = None
    episode.directions, episode.selected_direction = [], None
    episode.analysis_state = 'stale' if episode.evidence else 'empty'
    for version in db.scalars(select(Version).where(Version.episode_id == episode.id,
            Version.owner_id == episode.owner_id, Version.evidence_revision.is_not(None))):
        version.state, version.confirmed_at = 'restricted', None
        version.error = {'code': 'EVIDENCE_CHANGED', 'message': '来源证据已修订，请重新核对并生成版本。'}
        suspend_version_shares(db, [version.id], reason='EVIDENCE_CHANGED')


def build_edit_plan(direction, assets, packaging='standard'):
    """A bounded first cut following the selected proposal's ordered references.

    Sparse visual evidence is not interpolated into invented continuous events.
    The complete proposal stays in storage, including evidence not used by this cut.
    """
    narration = direction['title'] if packaging == 'standard' else ''
    limit = 59.9 if narration else 90  # 30 s voice plus AAC/frame boundary margin
    segments, remaining = [], float(limit)
    for span in direction['evidence']:
        asset = assets.get(span['asset_id'])
        if not asset or not 0 <= span['start'] < span['end'] <= asset.duration:
            raise DomainError('INVALID_PLAN', '编辑计划引用了无效素材或区间。', 409)
        length = min(span['end'] - span['start'], remaining)
        if length < .1 or len(segments) >= 30:
            break
        # Captions are scene descriptions, never presented as verbatim dialogue.
        note = span['note'] if packaging == 'standard' else ''
        caption = note if len(note) <= 100 else note[:99] + '…'
        segments.append({'asset_id': asset.id, 'start': span['start'],
                         'end': span['start'] + length, 'caption': caption})
        remaining -= length
    if not segments:
        raise DomainError('INSUFFICIENT_EVIDENCE', '证据不足以组成片段，请补充或修订。', 409)
    return segments, narration


class ModelServices:
    """Real adapters only. Dependency injection is confined to Python tests."""
    is_real = True

    def __init__(self, config):
        self.config = config

    def ready(self):
        # All contracts are checked before offering the paid workflow.
        from .vision import CloudVision
        from .speech import CloudSpeech
        if self.config.get('CLOUD_RETENTION_VERIFIED') is not True:
            return False, '真实 AI 接入按你的安排延至上线前；当前不发送素材。先核实供应商留存/删除期限与境内处理，再配置服务。'
        for service in (CloudDirector(self.config), CloudVision(self.config), CloudSpeech(self.config)):
            ready, reason = service.ready()
            if not ready:
                return False, reason
        return True, '已配置云端准入；分析仍需人工核对，所有费用受本期与累计预算限制。'

    def prepare_vision(self, assets, media_root):
        from .vision import prepare_video
        return prepare_video(assets, media_root=media_root)

    async def vision(self, prepared, reservation, permit):
        from .vision import CloudVision
        return await CloudVision(self.config, budget_check=permit).analyze(prepared, reservation=reservation)

    def prepare_audio(self, asset, path, directory):
        from .speech import extract_audio_chunks
        return extract_audio_chunks(path, directory, asset_id=asset.id)

    def asr_estimate(self, chunks):
        from .speech import asr_reservation_yuan
        return asr_reservation_yuan(chunks)

    async def asr(self, chunks, reservation, permit):
        from .speech import CloudSpeech
        return await CloudSpeech(self.config, budget_check=permit).transcribe(chunks, reservation=reservation)

    async def directions(self, evidence, assets, background, reservation, permit):
        return await CloudDirector(self.config, budget_check=permit).propose(evidence, assets, background, reservation=reservation)

    def tts_estimate(self, text):
        from .speech import tts_reservation_yuan
        return tts_reservation_yuan(text)

    async def tts(self, text, output, reservation, permit):
        from .speech import CloudSpeech
        return await CloudSpeech(self.config, budget_check=permit).synthesize(text, output, reservation=reservation)


class Pipeline:
    def __init__(self, repo, services):
        self.repo, self.services = repo, services

    def require_ready(self):
        ready, reason = self.services.ready()
        if not ready:
            raise DomainError('CLOUD_DISABLED', reason, 409)

    def _validate_job(self, db, job):
        episode = self.repo.owned(db, Episode, job.episode_id, job.owner_id)
        assets = assert_sources(db, episode, job.payload['asset_ids'])
        if job.payload['revision'] != episode.evidence_revision:
            raise DomainError('EVIDENCE_CHANGED', '证据已更新，此任务不能继续。', 409)
        if job.version_id:
            version = self.repo.owned(db, Version, job.version_id, job.owner_id)
            if version.state == 'restricted':
                raise DomainError('SOURCE_REMOVED', '版本已受限，任务不能继续。', 409)
        return episode, assets

    def invoke(self, job_id, kind, amount, operation, request_digest=None, checkpoint_key=None):
        """Reserve before network I/O; persist unknown costs even on crashes."""
        checkpoint_key = checkpoint_key or kind + ':' + (request_digest or 'default')
        self.require_ready()
        with self.repo.transaction() as db:
            job = db.get(Job, job_id)
            self._validate_job(db, job)
            if checkpoint_key and checkpoint_key in job.payload.get('steps', {}):
                return job.payload['steps'][checkpoint_key]
            held = db.scalars(select(Budget).where(Budget.episode_id == job.episode_id,
                Budget.owner_id == job.owner_id,
                (Budget.state.in_(('unknown', 'reserved'))) | ((Budget.state == 'settled') & Budget.cost.is_(None)))).first()
            if held:
                raise DomainError('COST_UNKNOWN', '本期上一步费用尚未核实，已暂停后续模型调用。', 409)
            reservation = self.repo.reserve(db, job.owner_id, job.episode_id, amount)
            call = ModelCall(owner_id=job.owner_id, episode_id=job.episode_id,
                             job_id=job.id, reservation_id=reservation, kind=kind,
                             request_digest=request_digest, usage={'real_call': bool(self.services.is_real)})
            db.add(call)
            db.flush()
            call_id = call.id
            job.payload = {**job.payload, 'reservation': reservation}

        def permit(candidate, required=None, digest=None):
            if candidate != reservation:
                return False
            self.require_ready()
            with self.repo.transaction() as db:
                row, ledger, current = db.get(ModelCall, call_id), db.get(Budget, reservation), db.get(Job, job_id)
                self._validate_job(db, current)
                if current.state != 'running' or ledger.state != 'reserved':
                    return False
                need = amount if required is None else required
                if not isinstance(need, (int, float)) or not math.isfinite(need) or ledger.amount < math.ceil(need * 1e6):
                    return False
                if digest is not None and row.request_digest != digest:
                    return False
                row.state = 'sending'
                return True

        started = time.monotonic()
        output, failure = None, None
        try:
            output = asyncio.run(operation(reservation, permit))
            usage = output['usage']
        except Exception as exc:
            failure = exc
            usage = getattr(exc, 'usage', None) or {'cost_yuan': None, 'cost_status': 'unknown'}
        with self.repo.transaction() as db:
            ledger, call = db.get(Budget, reservation), db.get(ModelCall, call_id)
            cost = usage.get('cost_yuan')
            # A preflight rejection definitely made no request.
            if call.state == 'prepared':
                cost = 0
            if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
                units = math.ceil(cost * 1e6)
                if units <= ledger.amount:
                    ledger.cost, ledger.state = units, 'settled'
                else:
                    ledger.state = 'unknown'
                    failure = DomainError('BILLING_MISMATCH', '模型费用超出预留口径，已停止并保留占用待核对。', 409)
            else:
                ledger.state = 'unknown'
            call.state = 'failed' if failure else 'succeeded'
            call.usage = {**usage, 'real_call': bool(self.services.is_real)}
            call.elapsed = round(time.monotonic() - started, 3)
            if not failure and checkpoint_key:
                result = output['result']
                result = result.model_dump() if hasattr(result, 'model_dump') else result
                job = db.get(Job, job_id)
                # Persist the paid result in the same transaction as settlement.
                # Publication to the current episode still revalidates revision.
                job.payload = {**job.payload, 'steps': {**job.payload.get('steps', {}), checkpoint_key: result}}
            self.repo.audit(db, call.owner_id, 'model_call_' + call.state, call.id,
                            kind=kind, cost_status=ledger.state)
        if failure:
            if isinstance(failure, DomainError):
                raise failure
            raise DomainError(getattr(failure, 'code', 'MODEL_FAILED'),
                getattr(failure, 'message', '模型处理失败，输入已保存；费用状态请查看任务记录。'), 409) from None
        return output['result']

    def checkpoint(self, job_id, name, value):
        with self.repo.transaction() as db:
            job = db.get(Job, job_id)
            self._validate_job(db, job)
            job.payload = {**job.payload, 'steps': {**job.payload.get('steps', {}), name: value}}

    def analyze(self, job_id):
        self.require_ready()
        with self.repo.factory() as db:
            job = db.get(Job, job_id)
            episode, assets = self._validate_job(db, job)
            steps = dict(job.payload.get('steps', {}))
        if 'vision' not in steps:
            self.stage(job_id, '正在抽帧并分析可见画面')
            prepared = self.services.prepare_vision([{'asset_id': a.id, 'path': self.repo.path(a.path)} for a in assets], self.repo.data_dir / 'assets')
            result = self.invoke(job_id, 'vision', prepared.required_reservation_yuan,
                lambda reservation, permit: self.services.vision(prepared, reservation, permit), prepared.request_digest, checkpoint_key='vision')
            steps['vision'] = result.model_dump() if hasattr(result, 'model_dump') else result
            self.checkpoint(job_id, 'vision', steps['vision'])
        for asset in assets:
            step = 'asr:' + asset.id
            if step in steps:
                continue
            if not asset.has_audio:
                steps[step] = []
                self.checkpoint(job_id, step, [])
                continue
            self.stage(job_id, '正在转写原声；结果仍需核对')
            from .artifacts import artifact_workspace
            with artifact_workspace(self.repo, job_id) as folder:
                chunks = self.services.prepare_audio(asset, self.repo.path(asset.path), folder)
                result = self.invoke(job_id, 'asr', self.services.asr_estimate(chunks),
                    lambda reservation, permit: self.services.asr(chunks, reservation, permit), checkpoint_key=step)
            steps[step] = result
            self.checkpoint(job_id, step, result)
        lengths = {a.id: a.duration for a in assets}
        evidence = []
        for observation in steps['vision']['observations']:
            t, aid = observation['time'], observation['asset_id']
            evidence.append({'id': uid(), 'asset_id': aid, 'start': max(0, t - 1),
                'end': min(lengths[aid], t + 1), 'note': observation['note'], 'kind': 'visual'})
        for asset in assets:
            for row in steps['asr:' + asset.id]:
                evidence.append({'id': uid(), 'asset_id': row['asset_id'], 'start': row['start'],
                                 'end': row['end'], 'note': row['text'], 'kind': 'speech'})
        notes = list(steps['vision'].get('uncertainties', []))
        # ASR emits word timings. Merge adjacent words from the same source
        # into reviewable utterances; retain every word and its outer time range.
        visual = [row for row in evidence if row['kind'] == 'visual']
        speech = []
        for row in (item for item in evidence if item['kind'] == 'speech'):
            if (speech and speech[-1]['asset_id'] == row['asset_id']
                    and row['start'] - speech[-1]['end'] <= 2
                    and row['end'] - speech[-1]['start'] <= 12
                    and len(speech[-1]['note']) + len(row['note']) < 500):
                speech[-1]['note'] += ' ' + row['note']
                speech[-1]['end'] = row['end']
            else:
                speech.append(dict(row))
        evidence = visual + speech
        if len(evidence) > 100:
            raise DomainError('EVIDENCE_TOO_LARGE', '可核对证据超过100条；已保留分析检查点，请缩小素材范围再核对，未截断内容。', 409)
        with self.repo.transaction() as db:
            job = db.get(Job, job_id)
            episode, _ = self._validate_job(db, job)
            invalidate_analysis(self.repo, db, episode)
            episode.evidence, episode.analysis_state = evidence, 'review'
            episode.analysis_asset_ids = job.payload['asset_ids']
            episode.analysis_notes = notes
            episode.analysis_coverage = steps['vision'].get('coverage', [])
            self.finish(db, job, {'evidence_count': len(evidence), 'revision': episode.evidence_revision}, '请核对画面描述和原声转写')

    def directions(self, job_id):
        with self.repo.factory() as db:
            job = db.get(Job, job_id)
            episode, assets = self._validate_job(db, job)
            if episode.evidence_confirmed_revision != episode.evidence_revision:
                raise DomainError('REVIEW_REQUIRED', '请先核对并确认证据。', 409)
            evidence = [{key: row[key] for key in ('asset_id', 'start', 'end', 'note')} for row in episode.evidence]
            background = episode.background
        self.stage(job_id, '正在根据已核对证据提出节目方向')
        result = self.invoke(job_id, 'directions', CloudDirector.required_reservation_yuan,
            lambda reservation, permit: self.services.directions(evidence, {a.id: a.duration for a in assets}, background, reservation, permit), checkpoint_key='directions')
        data = result.model_dump() if hasattr(result, 'model_dump') else result
        # Recheck trusted interface at the application boundary as well.
        valid = parse_result(json.dumps(data, ensure_ascii=False), {a.id: a.duration for a in assets})
        with self.repo.transaction() as db:
            job = db.get(Job, job_id)
            episode, _ = self._validate_job(db, job)
            episode.directions = [d.model_dump() for d in valid.directions]
            episode.directions_revision = episode.evidence_revision
            episode.analysis_notes = list(episode.analysis_notes) + valid.uncertainties
            self.finish(db, job, {'direction_count': len(valid.directions)}, '请选择有来源依据的节目方向')

    def narrate(self, job_id):
        with self.repo.factory() as db:
            job = db.get(Job, job_id)
            self._validate_job(db, job)
            version = db.get(Version, job.version_id)
            if not version.narration_approved or not version.narration_text.strip():
                raise DomainError('NARRATION_REVIEW_REQUIRED', '请先明确确认这版旁白文字。', 409)
            text = version.narration_text
        relative = 'outputs/' + uid() + '.wav'
        output = self.repo.path(relative)
        from .artifacts import record_artifact, release_artifact
        record_artifact(self.repo, job_id, relative)
        try:
            self.stage(job_id, '正在生成已确认文案的系统音色旁白')
            result = self.invoke(job_id, 'tts', self.services.tts_estimate(text),
                lambda reservation, permit: self.services.tts(text, output, reservation, permit), checkpoint_key='tts')
            from pathlib import Path
            actual = Path(result['path']).resolve()
            if actual.parent != self.repo.path('outputs') or not actual.is_file():
                raise DomainError('NARRATION_MISSING', '已生成的旁白文件缺失，请保存新版本后重试。', 409)
            output = actual
            relative = str(actual.relative_to(self.repo.data_dir))
            with self.repo.transaction() as db:
                job = db.get(Job, job_id)
                self._validate_job(db, job)
                version = db.get(Version, job.version_id)
                version.narration_path, version.state, version.error = relative, 'draft', None
                release_artifact(db, job, relative)
                self.finish(db, job, {'narration_ready': True}, '旁白已生成，可以渲染预览')
        except BaseException:
            # A successfully billed TTS file may be in a durable checkpoint.
            # Keep it for recovery; deletion is governed by source dependencies.
            raise

    def stage(self, job_id, label):
        with self.repo.transaction() as db:
            db.get(Job, job_id).stage = label

    def finish(self, db, job, result, label):
        job.state, job.stage, job.result, job.error = 'succeeded', label, result, None
        self.repo.audit(db, job.owner_id, job.kind + '_succeeded', job.id)

    def run(self, job_id, kind):
        {'analysis': self.analyze, 'directions': self.directions, 'narration': self.narrate}[kind](job_id)
