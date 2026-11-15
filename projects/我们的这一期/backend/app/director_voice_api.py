"""Authenticated owner recording endpoints; no transcription is auto-sent to chat."""
import asyncio
import hashlib
from pathlib import Path

from fastapi import Depends, File, Form, Request, UploadFile
from sqlalchemy import select

from .models import Episode, Job, now, uid
from .repository import DomainError
from .services import director_voice as voice
from .services.artifacts import artifact_workspace, record_artifact, release_artifact
from .services.media import MediaError


def register_director_voice(app, session, repository, services):
    prefix = '/api/v1/episodes/{episode_id}/director/voice'

    def owned_job(repo, db, episode_id, job_id, owner):
        repo.owned(db, Episode, episode_id, owner)
        job = repo.owned(db, Job, job_id, owner)
        if job.episode_id != episode_id or job.kind not in voice.VOICE_KINDS:
            raise DomainError('NOT_FOUND', '录音不存在或无权访问。', 404)
        return job

    @app.get(prefix + '/status', response_model=voice.VoiceStatus)
    def status(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth['user_id'])
        allowed, reason = voice.ready(services)
        return voice.VoiceStatus(can_record=allowed, reason=reason, mode='cloud' if services.is_real else 'demo')

    @app.get(prefix, response_model=voice.VoicesOut)
    def listing(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth['user_id'])
            jobs = db.scalars(select(Job).where(Job.episode_id == episode_id, Job.owner_id == auth['user_id'],
                Job.kind.in_(voice.VOICE_KINDS)).order_by(Job.created_at.desc()).limit(20))
            return voice.VoicesOut(items=[voice.out(job) for job in jobs])

    @app.get(prefix + '/by-client/{client_id}', response_model=voice.VoiceOut)
    def by_client(episode_id: str, client_id: str, auth=Depends(session), repo=Depends(repository)):
        if not voice.CLIENT_ID.fullmatch(client_id):
            raise DomainError('INVALID_REQUEST', '录音请求编号无效。', 422)
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth['user_id'])
            job = db.scalars(select(Job).where(Job.owner_id == auth['user_id'],
                Job.idempotency_key == f'director_voice:{episode_id}:{client_id}')).first()
            if job is None or job.kind not in voice.VOICE_KINDS:
                raise DomainError('NOT_FOUND', '尚未找到这段录音。', 404)
            return voice.out(job)

    @app.get(prefix + '/{voice_id}', response_model=voice.VoiceOut)
    def get_voice(episode_id: str, voice_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            return voice.out(owned_job(repo, db, episode_id, voice_id, auth['user_id']))

    @app.post(prefix, response_model=voice.VoiceOut, status_code=201)
    async def upload(episode_id: str, request: Request, file: UploadFile = File(...),
            client_id: str = Form(...), confirm_send: bool = Form(False),
            accept_unknown_retention: bool = Form(False), auth=Depends(session), repo=Depends(repository)):
        job_id = target = None
        published = False
        try:
            if not voice.CLIENT_ID.fullmatch(client_id):
                raise DomainError('INVALID_REQUEST', '录音请求编号无效。', 422)
            if not confirm_send or not accept_unknown_retention:
                raise DomainError('VOICE_CONSENT_REQUIRED', '请确认将本次录音交给MiniMax转写，知悉供应商留存期限尚未核实。', 409)
            voice.require_ready(services)
            suffix = Path((file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]).suffix.lower()
            if suffix not in ('.wav', '.webm', '.mp4', '.m4a'):
                raise DomainError('VOICE_INVALID_FILE', '录音仅支持WAV、WebM或M4A/MP4音频。', 422)
            key = f'director_voice:{episode_id}:{client_id}'
            with repo.transaction() as db:
                repo.owned(db, Episode, episode_id, auth['user_id'])
                previous = db.scalars(select(Job).where(Job.owner_id == auth['user_id'], Job.idempotency_key == key)).first()
                if previous:
                    previous_digest = previous.payload.get('source_sha256')
                    if not previous_digest:
                        raise DomainError('VOICE_UPLOAD_PENDING', '这段录音仍在接收或接收未完成，请先核对已保存状态。', 409)
                    previous_id = previous.id
                else:
                    previous_id = None
                    existing = list(db.scalars(select(Job).where(Job.episode_id == episode_id,
                        Job.owner_id == auth['user_id'], Job.kind.in_(voice.VOICE_KINDS))))
                    if any(row.state in ('running', 'queued') for row in existing):
                        raise DomainError('VOICE_BUSY', '上一段语音还在处理，请稍后再提交。', 409)
                    if len(existing) >= 50:
                        raise DomainError('VOICE_LIMIT', '本期最多保存50段导演语音，已有文字仍可继续使用。', 409)
                    job = Job(owner_id=auth['user_id'], episode_id=episode_id, kind='director_voice_upload',
                        state='running', stage='正在接收录音', idempotency_key=key, payload={
                            'client_id': client_id, 'mode': 'cloud' if services.is_real else 'demo',
                            'voice_consent': {'confirm_send': True, 'accept_unknown_retention': True,
                                'owner_id': auth['user_id'], 'episode_id': episode_id, 'client_id': client_id,
                                'provider': 'minimax', 'model': 'asr-1.0', 'accepted_at': now()}})
                    db.add(job); db.flush(); job_id = job.id
            if previous_id:
                checksum, size = hashlib.sha256(), 0
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > voice.MAX_BYTES:
                        raise DomainError('VOICE_TOO_LARGE', '每段录音最多12MB。', 413)
                    checksum.update(chunk)
                if checksum.hexdigest() != previous_digest:
                    raise DomainError('VOICE_REQUEST_CONFLICT', '同一请求编号对应的录音已变化，请重新录制提交。', 409)
                with repo.factory() as db:
                    return voice.out(owned_job(repo, db, episode_id, previous_id, auth['user_id']))
            with artifact_workspace(repo, job_id) as directory:
                raw, normalised = directory / ('source' + suffix), directory / 'normalised.wav'
                async with request.app.state.upload_lock:
                    checksum, size = hashlib.sha256(), 0
                    with raw.open('xb') as output:
                        while chunk := await file.read(1024 * 1024):
                            size += len(chunk)
                            if size > voice.MAX_BYTES:
                                raise DomainError('VOICE_TOO_LARGE', '每段录音最多12MB。', 413)
                            checksum.update(chunk); output.write(chunk)
                    if not size:
                        raise DomainError('VOICE_INVALID_FILE', '录音为空，请重新录制。', 422)
                    duration = await asyncio.to_thread(voice.normalise, raw, normalised, suffix)
                    normalised_digest = await asyncio.to_thread(voice.digest, normalised)
                    relative = 'outputs/' + uid() + '.wav'
                    record_artifact(repo, job_id, relative)
                    target = repo.data_dir / relative
                    with repo.transaction() as db:
                        repo.owned(db, Episode, episode_id, auth['user_id'])
                        job = db.get(Job, job_id)
                        if job.state != 'running':
                            raise DomainError('VOICE_UNAVAILABLE', '录音任务已失效。', 409)
                        normalised.rename(target)
                        job.payload = {**job.payload, 'source_sha256': checksum.hexdigest(),
                            'voice_path': relative, 'voice_sha256': normalised_digest, 'duration': duration,
                            'voice_consent': {**job.payload['voice_consent'], 'voice_sha256': normalised_digest}}
                        job.kind, job.state, job.stage = 'director_voice', 'queued', '等待语音转写'
                        release_artifact(db, job, relative)
                        repo.audit(db, auth['user_id'], 'director_voice_submitted', job.id, seconds=round(duration, 3))
                    published = True
            with repo.factory() as db:
                return voice.out(owned_job(repo, db, episode_id, job_id, auth['user_id']))
        except BaseException as exc:
            if target and not published:
                target.unlink(missing_ok=True)
            if job_id and not published:
                error = {'code': getattr(exc, 'code', 'VOICE_UPLOAD_INTERRUPTED'),
                         'message': getattr(exc, 'message', '录音接收未完成，未发送到模型。')}
                with repo.transaction() as db:
                    job = db.get(Job, job_id)
                    job.state, job.stage, job.error = 'failed', '录音未提交', error
            if isinstance(exc, MediaError):
                raise DomainError(exc.code, exc.message, 422) from None
            raise
        finally:
            await file.close()
