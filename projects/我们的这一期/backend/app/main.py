import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, File, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from starlette.exceptions import HTTPException
from .config import BACKEND, Settings
from .body_limit import BodyLimitMiddleware
from .models import Asset, Budget, Episode, Job, ModelCall, Session, Version, now, uid
from .repository import DomainError, Repository
from .schemas import (AssetOut, AuthOut, EpisodeIn, EpisodeOut, EpisodesOut, ImpactOut,
                      JobOut, Login, Ok, StatusOut, VersionIn, VersionOut,
                      EvidenceReview, DirectionSelect, NarrationConfirm, RetentionOut, Segment,
                      DirectorMessageIn, DirectorState, DirectorChatIn, DirectorChatState, DirectorActionIn, DirectorActionOut,
                      SubtitleTrackOut)
from .services import media
from .services.cleanup import cleanup_deleted_files
from .services.cleanup_status import pending_cleanup, asset_cleanup_pending
from .services.pipeline import (Pipeline, ModelServices, active_assets, assert_sources,
                                invalidate_analysis, build_edit_plan)
from .worker import Worker, source_segments
from .collaboration_api import register_collaboration
from .services.collaboration import guard_asset, asset_details, shared_quota, cleanup_collaboration
from .sharing_api import register_sharing
from .director_voice_api import register_director_voice
from .sharing_models import Share
from .services.sharing import suspend_version_shares
from .services import workflow_access
from .services import retention
from .services import director_guidance, director_chat
from .chat_models import DirectorChatTurn
from .services.artifacts import artifact_workspace
from .services.edit_contract import is_director_edit, check_edit, mark_edit
from .services.editing import validate_transitions, validate_speech_boundaries
from .retention_models import Retention

COOKIE = 'episode_session'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def create_app(settings=None, *, services=None):
    settings = settings or Settings.from_env()
    model_services = services or ModelServices(settings.cloud)
    cookie_name = COOKIE if model_services.is_real else 'episode_demo_session'

    @asynccontextmanager
    async def lifespan(app):
        repo = Repository(settings.data_dir)
        app.state.repo = repo
        if isinstance(model_services, ModelServices):
            model_services.repo = repo
        app.state.upload_lock = asyncio.Semaphore(2)
        # A prior process may have stopped after revoking access but before
        # unlinking files. Retry only references already marked for deletion.
        cleanup = await asyncio.to_thread(cleanup_deleted_files, repo)
        await asyncio.to_thread(cleanup_collaboration, repo)
        app.state.pending_cleanup = cleanup.pending
        if cleanup.pending:
            logging.getLogger(__name__).warning(
                'Local deletion cleanup pending: %d application files', cleanup.pending)
        app.state.pipeline = Pipeline(repo, model_services)
        worker = Worker(repo, pipeline=app.state.pipeline)
        if settings.worker_enabled:
            worker.start()
        try:
            yield
        finally:
            if settings.worker_enabled:
                await asyncio.to_thread(worker.close)
            repo.engine.dispose()

    app = FastAPI(title='我们的这一期 · 本地验收', version='0.1.0', lifespan=lifespan)
    app.state.settings = settings

    @app.middleware('http')
    async def local_boundary(request, call_next):
        host = request.url.hostname
        if host not in ('127.0.0.1', 'localhost', '::1', 'testserver'):
            return JSONResponse({'error': {'code': 'INVALID_HOST', 'message': '仅支持本机访问。'}}, 400)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            expected = str(request.base_url).rstrip('/')
            if origin and origin != expected:
                return JSONResponse({'error': {'code': 'INVALID_ORIGIN', 'message': '请求来源不匹配。'}}, 403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': exc.message}}, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid(_, exc):
        return JSONResponse({'error': {'code': 'INVALID_REQUEST', 'message': '输入格式或范围无效，请检查后重试。'}}, 422)

    @app.exception_handler(HTTPException)
    async def http_error(_, exc):
        return JSONResponse({'error': {'code': 'HTTP_ERROR', 'message': '请求未能处理。'}}, exc.status_code)

    @app.exception_handler(Exception)
    async def unknown(_, exc):
        return JSONResponse({'error': {'code': 'INTERNAL_ERROR', 'message': '处理未完成，请稍后重试。'}}, 500)

    def repository(request: Request):
        return request.app.state.repo

    def session(request: Request, repo=Depends(repository)):
        token = request.cookies.get(cookie_name, '')
        with repo.factory() as db:
            row = db.get(Session, digest(token)) if token else None
            if not row or row.expires < time.time():
                raise DomainError('AUTH_REQUIRED', '请使用邀请码登录。', 401)
            if request.method not in ('GET', 'HEAD') and not hmac.compare_digest(
                    request.headers.get('x-csrf-token', ''), row.csrf):
                raise DomainError('CSRF_FAILED', '会话校验失败，请刷新后重试。', 403)
            return {'user_id': row.owner_id, 'csrf_token': row.csrf, 'token_hash': row.token_hash}

    def active_asset(db, repo, asset_id, owner):
        asset = repo.owned(db, Asset, asset_id, owner)
        if asset.deleted:
            raise DomainError('NOT_FOUND', '素材已删除。', 404)
        return asset

    def asset_out(db, asset):
        return AssetOut(id=asset.id, filename=asset.filename, duration=asset.duration,
                        width=asset.width, height=asset.height, has_audio=asset.has_audio,
                        size=asset.size, media_url=f'/api/v1/assets/{asset.id}/media',
                        **asset_details(db, asset))

    def version_out(version):
        return VersionOut(id=version.id, title=version.title, state=version.state,
                          origin=version.origin, segments=version.segments,
                          media_url=f'/api/v1/versions/{version.id}/media' if version.state in ('ready', 'confirmed') else None,
                          job_id=version.job_id, error=version.error,
                          base_version_id=version.base_version_id, created_at=version.created_at,
                          narration_text=version.narration_text, narration_approved=version.narration_approved,
                          narration_ready=bool(version.narration_path), packaging=version.packaging,
                          evidence_revision=version.evidence_revision)

    def job_out(job):
        return JobOut(id=job.id, kind=job.kind, state=job.state, stage=job.stage,
                      error=job.error, version_id=job.version_id, result=job.result)

    def episode_out(db, repo, episode):
        filters = lambda model: (model.episode_id == episode.id, model.owner_id == episode.owner_id)
        usage = shared_quota(db, episode.id)
        return EpisodeOut(id=episode.id, title=episode.title, background=episode.background, created_at=episode.created_at,
                          retention=retention.status(db, episode), director=director_guidance.state(db, episode),
                          workflow=workflow_access.capability(model_services, repo, episode.id, episode.owner_id),
                          assets=[asset_out(db, a) for a in db.scalars(select(Asset).where(*filters(Asset), Asset.deleted == False).order_by(Asset.created_at))],
                          versions=[version_out(v) for v in db.scalars(select(Version).where(*filters(Version)).order_by(Version.created_at))],
                          jobs=[job_out(j) for j in db.scalars(select(Job).where(*filters(Job), Job.kind != 'asset_upload').order_by(Job.created_at))],
                          directions=episode.directions, budget=repo.budget(db, episode.id),
                          quota={'original_count': usage['count'], 'total_seconds': usage['seconds'],
                                 'total_bytes': usage['bytes'], 'max_count': settings.max_assets,
                                 'max_seconds': settings.max_total_seconds, 'max_bytes': settings.max_upload_bytes},
                          pending_cleanup=pending_cleanup(db, repo, episode),
                          evidence=episode.evidence, evidence_revision=episode.evidence_revision,
                          evidence_confirmed_revision=episode.evidence_confirmed_revision,
                          analysis_state=episode.analysis_state, analysis_notes=episode.analysis_notes,
                          analysis_coverage=episode.analysis_coverage, directions_revision=episode.directions_revision,
                          selected_direction=episode.selected_direction)

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(BACKEND / 'static/index.html', media_type='text/html')

    @app.get('/collaboration', include_in_schema=False)
    def collaboration_page():
        return FileResponse(BACKEND / 'static/collaboration.html', media_type='text/html')

    @app.get('/participant', include_in_schema=False)
    def participant_page():
        return FileResponse(BACKEND / 'static/participant.html', media_type='text/html')

    @app.get('/collaboration.css', include_in_schema=False)
    def collaboration_styles():
        return FileResponse(BACKEND / 'static/collaboration.css', media_type='text/css')

    @app.get('/collaboration-common.js', include_in_schema=False)
    def collaboration_common():
        return FileResponse(BACKEND / 'static/collaboration-common.js', media_type='text/javascript')

    @app.get('/sharing', include_in_schema=False)
    def sharing_page():
        return FileResponse(BACKEND / 'static/sharing.html', media_type='text/html')

    @app.get('/watch/{share_id}', include_in_schema=False)
    def watch_page(share_id: str):
        return FileResponse(BACKEND / 'static/watch.html', media_type='text/html')

    @app.get('/sharing.js', include_in_schema=False)
    def sharing_script():
        return FileResponse(BACKEND / 'static/sharing.js', media_type='text/javascript')

    @app.get('/watch.js', include_in_schema=False)
    def watch_script():
        return FileResponse(BACKEND / 'static/watch.js', media_type='text/javascript')

    @app.get('/api/v1/status', response_model=StatusOut)
    def status():
        ready, reason = model_services.ready()
        return StatusOut(cloud_enabled=ready and model_services.is_real, cloud_reason=reason,
                         workflow_enabled=ready, model_mode='cloud' if model_services.is_real else 'demo',
                         media_ready=bool(shutil.which('ffmpeg') and shutil.which('ffprobe')), stage='阶段3-1')

    @app.post('/api/v1/auth/login', response_model=AuthOut)
    def login(body: Login, response: Response, repo=Depends(repository)):
        owner = next((v for k, v in settings.invites.items() if hmac.compare_digest(body.invite_code.encode(), k.encode())), None)
        if not owner:
            raise DomainError('INVALID_INVITE', '邀请码不正确。', 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with repo.transaction() as db:
            db.add(Session(token_hash=digest(token), owner_id=owner, csrf=csrf, expires=time.time() + settings.session_seconds))
            repo.audit(db, owner, 'login', 'session')
        response.set_cookie(cookie_name, token, max_age=settings.session_seconds, httponly=True, samesite='strict', path='/')
        return AuthOut(user_id=owner, csrf_token=csrf)

    @app.get('/api/v1/auth/me', response_model=AuthOut)
    def me(auth=Depends(session)):
        return AuthOut(user_id=auth['user_id'], csrf_token=auth['csrf_token'])

    @app.post('/api/v1/auth/logout', response_model=Ok)
    def logout(response: Response, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            db.delete(db.get(Session, auth['token_hash']))
        response.delete_cookie(cookie_name, path='/')
        return Ok()

    @app.get('/api/v1/episodes', response_model=EpisodesOut)
    def episodes(auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            return EpisodesOut(items=[episode_out(db, repo, e) for e in db.scalars(select(Episode).where(Episode.owner_id == auth['user_id']).order_by(Episode.created_at))
                                     if not retention.unavailable(db, e.id)])

    @app.post('/api/v1/episodes', response_model=EpisodeOut, status_code=201)
    def create_episode(body: EpisodeIn, auth=Depends(session), repo=Depends(repository)):
        if not body.title.strip():
            raise DomainError('INVALID_TITLE', '请填写节目名称。')
        with repo.transaction() as db:
            episode = Episode(owner_id=auth['user_id'], title=body.title.strip(), background=body.background)
            db.add(episode)
            db.flush()
            db.add(Retention(episode_id=episode.id, owner_id=episode.owner_id, expires_at=time.time() + 30 * 86400))
            db.flush()
            repo.audit(db, auth['user_id'], 'episode_created', episode.id)
            return episode_out(db, repo, episode)

    @app.get('/api/v1/episodes/{episode_id}', response_model=EpisodeOut)
    def get_episode(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            return episode_out(db, repo, repo.owned(db, Episode, episode_id, auth['user_id']))

    @app.get('/api/v1/episodes/{episode_id}/retention', response_model=RetentionOut)
    def retention_status(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            episode = db.get(Episode, episode_id)
            if not episode or episode.owner_id != auth['user_id']:
                raise DomainError('NOT_FOUND', '内容不存在或无权访问。', 404)
            return retention.status(db, episode)

    @app.delete('/api/v1/episodes/{episode_id}', response_model=Ok)
    def delete_episode(episode_id: str, confirm: bool = False, auth=Depends(session), repo=Depends(repository)):
        if not confirm:
            raise DomainError('CONFIRM_REQUIRED', '删除整期会关闭全部分享并清理素材、采访和成片，请先确认。', 409)
        retention.request_deletion(repo, episode_id, auth['user_id'])
        retention.purge(repo, episode_id)
        return {'ok': True}

    @app.post('/api/v1/episodes/{episode_id}/assets', response_model=AssetOut, status_code=201)
    async def upload(episode_id: str, request: Request, file: UploadFile = File(...), auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth['user_id'])
        name = re.sub(r'[\x00-\x1f\x7f]', '', (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1])[:200]
        suffix = Path(name).suffix.lower()
        if suffix not in ('.mp4', '.mov'):
            await file.close()
            raise DomainError('INVALID_FORMAT', '仅支持 MP4 或 MOV 视频。')
        with repo.transaction() as db:
            repo.owned(db, Episode, episode_id, auth['user_id'])
            upload_job = Job(owner_id=auth['user_id'], episode_id=episode_id, kind='asset_upload',
                             state='running', stage='正在接收素材', idempotency_key='upload:' + uid(), payload={})
            db.add(upload_job)
            db.flush()
            upload_job_id = upload_job.id
        workspace = artifact_workspace(repo, upload_job_id)
        temp = target = None
        workspace_entered = False
        succeeded = False
        try:
            directory = workspace.__enter__()
            workspace_entered = True
            temp = directory / ('source' + suffix)
            async with request.app.state.upload_lock:
                size = 0
                with temp.open('xb') as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            raise DomainError('UPLOAD_TOO_LARGE', '素材总大小不能超过 1 GB。', 413)
                        output.write(chunk)
                info = await asyncio.to_thread(media.probe, temp)
                with repo.transaction() as db:
                    repo.owned(db, Episode, episode_id, auth['user_id'])
                    quota = shared_quota(db, episode_id)
                    if quota['count'] >= settings.max_assets:
                        raise DomainError('TOO_MANY_ASSETS', '每期最多上传 8 段视频。', 409)
                    if quota['bytes'] + size > settings.max_upload_bytes:
                        raise DomainError('UPLOAD_TOO_LARGE', '素材总大小不能超过 1 GB。', 413)
                    if quota['seconds'] + info['duration'] > settings.max_total_seconds:
                        raise DomainError('DURATION_EXCEEDED', '素材总时长不能超过 10 分钟。', 409)
                    relative = 'assets/' + uid() + suffix
                    target = repo.path(relative)
                    temp.rename(target)
                    asset = Asset(owner_id=auth['user_id'], episode_id=episode_id, filename=name, path=relative, size=size,
                                  **{k: info[k] for k in ('duration', 'width', 'height', 'has_audio')})
                    db.add(asset)
                    db.flush()
                    episode = db.get(Episode, episode_id)
                    invalidate_analysis(repo, db, episode)
                    repo.audit(db, auth['user_id'], 'asset_uploaded', asset.id, bytes=size)
                    result = asset_out(db, asset)
                succeeded = True
                return result
        except media.MediaError as exc:
            if target:
                target.unlink(missing_ok=True)
            raise DomainError(exc.code, exc.message, 422) from None
        except BaseException:
            if target:
                target.unlink(missing_ok=True)
            raise
        finally:
            try:
                if workspace_entered:
                    workspace.__exit__(None, None, None)
            finally:
                with repo.transaction() as db:
                    job = db.get(Job, upload_job_id)
                    job.state = 'succeeded' if succeeded else 'failed'
                    job.stage = '素材已接收' if succeeded else '上传未完成'
                await file.close()

    @app.api_route('/api/v1/assets/{asset_id}/media', methods=['GET', 'HEAD'])
    def asset_media(asset_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            asset = active_asset(db, repo, asset_id, auth['user_id'])
            guard_asset(db, asset)
            path = repo.path(asset.path)
            if not path.is_file():
                raise DomainError('MEDIA_MISSING', '应用副本丢失，请重新上传。', 404)
            return FileResponse(path, media_type='video/mp4')

    @app.post('/api/v1/episodes/{episode_id}/versions', response_model=VersionOut, status_code=201)
    def create_version(episode_id: str, body: VersionIn, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            base = None
            if body.base_version_id:
                base = repo.owned(db, Version, body.base_version_id, auth['user_id'])
                if base.episode_id != episode_id:
                    raise DomainError('INVALID_BASE', '原版本不属于本期。')
            if sum(s.end - s.start for s in body.segments) > 90:
                raise DomainError('OUTPUT_TOO_LONG', '导出总时长不能超过 90 秒。')
            stored_segments = [s.model_dump(exclude_defaults=True) for s in body.segments]
            # Caption is historically required in the public response and on disk.
            for segment, source in zip(stored_segments, body.segments):
                segment['caption'] = source.caption
            guarded = bool(base and is_director_edit(db, base)) or any(s.evidence_ids or s.purpose or s.editorial for s in body.segments)
            if guarded:
                check_edit(stored_segments, episode)
            protected_segments = []
            for s in body.segments:
                asset = active_asset(db, repo, s.asset_id, auth['user_id'])
                if asset.episode_id != episode_id or s.end > asset.duration + .001:
                    raise DomainError('INVALID_SEGMENT', '片段超出本期素材范围。')
                guard_asset(db, asset, s.model_dump())
                if s.visual_beats and asset_details(db, asset)['fixed_segment'] is not None:
                    raise DomainError('INVALID_VISUAL_BEAT', '采访表达保持原样，不能添加人物或物件视觉强调。', 422)
                if s.editorial and s.editorial.treatment != 'none' and asset_details(db, asset)['fixed_segment'] is not None:
                    raise DomainError('INVALID_EDITORIAL', '采访表达保持原样，不添加编导包装。', 422)
                protected_segments.append({**s.model_dump(), '_preserve_expression': asset_details(db, asset)['fixed_segment'] is not None})
            try:
                validate_transitions(protected_segments)
                if any(s.transition != 'cut' for s in body.segments):
                    validate_speech_boundaries(stored_segments, episode.evidence)
                if any(s.get('subtitle_edits') for s in stored_segments):
                    if body.packaging != 'variety_v2':
                        raise DomainError('SUBTITLE_STYLE_REQUIRED','字幕校对需保留综艺节奏包装。',422)
                    from .services.dialogue_captions import attach_dialogue, apply_subtitle_edits
                    attach_dialogue(db,episode,protected_segments)
                    apply_subtitle_edits(protected_segments)
            except ValueError as exc:
                raise DomainError('INVALID_TRANSITION', str(exc), 422) from None
            version = Version(owner_id=auth['user_id'], episode_id=episode_id, title=body.title,
                              segments=stored_segments, base_version_id=body.base_version_id,
                              origin=base.origin if base and base.origin in ('ai', 'demo') else 'manual',
                              evidence_revision=episode.evidence_revision if guarded else base.evidence_revision if base else None,
                              direction_id=base.direction_id if base else None,
                              narration_text=body.narration_text if body.packaging in ('standard', 'variety', 'variety_v2') else '',
                              packaging=body.packaging)
            if base and base.state == 'restricted' and (base.error or {}).get('code') != 'COLLAB_RESTRICTED':
                raise DomainError('VERSION_UNAVAILABLE', '旧版本证据已失效，请从当前素材重新建立版本。', 409)
            # A packaging/title-only revision can reuse an already approved voice.
            # Never carry approval across a text, source span or caption change.
            if (base and base.state in ('ready', 'confirmed') and not base.error
                    and base.narration_approved and base.narration_path
                    and version.narration_text and version.narration_text == base.narration_text
                    and [{k:v for k,v in Segment.model_validate(s).model_dump().items() if k!='subtitle_edits'} for s in version.segments]
                        == [{k:v for k,v in Segment.model_validate(s).model_dump().items() if k!='subtitle_edits'} for s in base.segments]):
                source_segments(repo, db, base)
                from .services.packaging import reusable_narration
                try:
                    reusable_narration(repo.data_dir, base.narration_path)
                except media.MediaError as exc:
                    raise DomainError(exc.code, exc.message, 409) from None
                version.narration_path = base.narration_path
                version.narration_approved = True
            db.add(version)
            db.flush()
            if guarded:
                mark_edit(repo, db, version)
            repo.audit(db, auth['user_id'], 'version_created', version.id)
            if version.narration_path:
                repo.audit(db, auth['user_id'], 'narration_reused', version.id, base_version_id=base.id)
            return version_out(version)

    @app.post('/api/v1/versions/{version_id}/review', response_model=JobOut)
    def review_output(version_id: str, auth=Depends(session), repo=Depends(repository)):
        from .services.render_review import enqueue
        with repo.transaction() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            return job_out(enqueue(repo, db, version))

    @app.post('/api/v1/versions/{version_id}/visual-review', response_model=JobOut)
    def review_visual_output(version_id: str, auth=Depends(session), repo=Depends(repository)):
        from .services.visual_review import enqueue
        with repo.transaction() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            return job_out(enqueue(repo, db, version, model_services))

    @app.get('/api/v1/versions/{version_id}/subtitles', response_model=SubtitleTrackOut)
    def subtitle_track(version_id: str, auth=Depends(session), repo=Depends(repository)):
        from .services.dialogue_captions import review_flags
        with repo.factory() as db:
            version=repo.owned(db,Version,version_id,auth['user_id'])
            if version.state=='restricted':
                raise DomainError('VERSION_UNAVAILABLE','本版来源已受限，不能校对字幕。',409)
            if version.packaging!='variety_v2':
                raise DomainError('SUBTITLE_STYLE_REQUIRED','请在综艺节奏版本中校对字幕。',422)
            segments=source_segments(repo,db,version,subtitle_review=True)
            rows=[]
            for index,segment in enumerate(segments):
                for row in segment.get('_dialogue',[]):
                    rows.append(dict(cue_id=row['cue_id'],segment_index=index,asset_id=segment['asset_id'],
                        start=row['start'],end=row['end'],text=row['text'],original_text=row['original_text'],
                        source_text=row.get('context',''),hidden=row['hidden'],reviewed=row['reviewed'],
                        locked=segment.get('_preserve_expression',False),flags=review_flags(row),
                        media_url=f"/api/v1/assets/{segment['asset_id']}/media"))
            return {'version_id':version.id,'rows':rows}

    @app.post('/api/v1/versions/{version_id}/director-run/{action}', response_model=JobOut)
    def control_director_run(version_id: str, action: str, auth=Depends(session), repo=Depends(repository)):
        from .services.director_run import control
        with repo.transaction() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            return job_out(control(repo, db, version, action))

    @app.post('/api/v1/versions/{version_id}/render', response_model=JobOut)
    def render_version(version_id: str, auth=Depends(session), repo=Depends(repository)):
        if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
            raise DomainError('MEDIA_UNAVAILABLE', '请先安装 FFmpeg。', 503)
        with repo.transaction() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            from .services.director_run import require_manual_allowed
            require_manual_allowed(db, version)
            if version.state == 'restricted':
                raise DomainError('SOURCE_REMOVED', '素材已删除，无法渲染该版本。', 409)
            source_segments(repo, db, version)
            if version.narration_text and not version.narration_path:
                raise DomainError('NARRATION_REQUIRED', '请先核对并生成旁白，或保存一个不含旁白的新版本。', 409)
            if version.state == 'voicing':
                raise DomainError('JOB_ACTIVE', '旁白仍在处理中，请稍后渲染。', 409)
            if version.job_id:
                existing = db.get(Job, version.job_id)
                if existing and existing.kind == 'render' and existing.state in ('queued', 'running', 'succeeded'):
                    return job_out(existing)
            job = Job(owner_id=auth['user_id'], episode_id=version.episode_id, version_id=version.id,
                      kind='render', idempotency_key='render:' + version.id + ':' + uid())
            db.add(job)
            db.flush()
            version.job_id, version.state, version.error = job.id, 'rendering', None
            suspend_version_shares(db, [version.id], reason='CONTENT_CHANGED')
            repo.audit(db, auth['user_id'], 'render_queued', job.id)
            return job_out(job)

    @app.get('/api/v1/jobs/{job_id}', response_model=JobOut)
    def get_job(job_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            return job_out(repo.owned(db, Job, job_id, auth['user_id']))

    @app.post('/api/v1/versions/{version_id}/confirm', response_model=VersionOut)
    def confirm(version_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            if version.state not in ('ready', 'confirmed') or not version.output_path or not repo.path(version.output_path).is_file():
                raise DomainError('NOT_READY', '请先生成并预览成功的版本。', 409)
            source_segments(repo, db, version)
            if version.state == 'confirmed' and version.confirmed_at:
                return version_out(version)
            version.state, version.confirmed_at = 'confirmed', now()
            repo.audit(db, auth['user_id'], 'version_confirmed', version.id)
            return version_out(version)

    def serve_version(version_id, auth, repo, export=False):
        with repo.factory() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            allowed = ('confirmed',) if export else ('ready', 'confirmed')
            if version.state not in allowed or not version.output_path:
                raise DomainError('VERSION_UNAVAILABLE', '请先预览并确认此版本；受限制版本不可导出。', 409)
            source_segments(repo, db, version)
            path = repo.path(version.output_path)
            if not path.is_file():
                raise DomainError('MEDIA_MISSING', '成片副本丢失，请重新生成。', 404)
            return FileResponse(path, media_type='video/mp4', filename='episode-' + version.id[:8] + '.mp4' if export else None)

    @app.api_route('/api/v1/versions/{version_id}/media', methods=['GET', 'HEAD'])
    def version_media(version_id: str, auth=Depends(session), repo=Depends(repository)):
        return serve_version(version_id, auth, repo)

    @app.api_route('/api/v1/versions/{version_id}/export', methods=['GET', 'HEAD'])
    def export_version(version_id: str, auth=Depends(session), repo=Depends(repository)):
        return serve_version(version_id, auth, repo, True)

    def affected(db, asset):
        from .collaboration_models import Adoption
        family = {asset.id}
        adoption = db.scalars(select(Adoption).where(Adoption.asset_id == asset.id)).first()
        if adoption:
            family.update(db.scalars(select(Asset.id).join(Adoption, Adoption.asset_id == Asset.id).where(
                Adoption.submission_id == adoption.submission_id, Asset.owner_id == asset.owner_id,
                Asset.episode_id == asset.episode_id)))
        return [v for v in db.scalars(select(Version).where(Version.episode_id == asset.episode_id, Version.owner_id == asset.owner_id))
                if any(s['asset_id'] in family for s in v.segments)]

    @app.get('/api/v1/assets/{asset_id}/impact', response_model=ImpactOut)
    def impact(asset_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            asset = active_asset(db, repo, asset_id, auth['user_id'])
            versions = [v.id for v in affected(db, asset)]
            shares = [{'id': s.id, 'label': s.label, 'version_id': s.version_id, 'state': s.state}
                      for s in db.scalars(select(Share).where(Share.owner_id == auth['user_id'],
                          Share.version_id.in_(versions), Share.state.in_(('active', 'restricted'))))]
            return ImpactOut(asset_id=asset.id, versions=versions, shares=shares,
                             message='将删除应用中的素材副本与关联成片，关联版本停止预览和导出；外部源视频不受影响。')

    @app.delete('/api/v1/assets/{asset_id}', response_model=Ok)
    def delete_asset(asset_id: str, confirm: bool = False, auth=Depends(session), repo=Depends(repository)):
        if not confirm:
            raise DomainError('CONFIRM_REQUIRED', '请先查看删除影响并明确确认。', 409)
        with repo.transaction() as db:
            # An already revoked asset remains addressable to its owner for
            # physical-cleanup retries; it is still unavailable to media APIs.
            asset = repo.owned(db, Asset, asset_id, auth['user_id'])
            asset.deleted = True
            dependent_versions = affected(db, asset)
            for version in dependent_versions:
                version.state, version.confirmed_at = 'restricted', None
                version.error = {'code': 'SOURCE_REMOVED', 'message': '引用素材已删除，该版本不可使用。'}
            suspend_version_shares(db, [v.id for v in dependent_versions], reason='SOURCE_REMOVED')
            episode = db.get(Episode, asset.episode_id)
            invalidate_analysis(repo, db, episode)
            episode.directions, episode.evidence, episode.selected_direction = [], [], None
            episode.analysis_notes, episode.analysis_coverage, episode.analysis_asset_ids = [], [], []
            dependent_ids = {version.id for version in dependent_versions}
            for job in db.scalars(select(Job).where(Job.episode_id == episode.id, Job.owner_id == auth['user_id'])):
                if asset.id in job.payload.get('asset_ids', []) or job.version_id in dependent_ids:
                    voice_path = job.payload.get('steps', {}).get('tts', {}).get('path')
                    # Erase private derived text while retaining only the exact
                    # cleanup reference needed for crash-safe physical deletion.
                    steps = {'tts': {'path': voice_path}} if voice_path else {}
                    job.payload = {**job.payload, 'steps': steps}
                    if job.state in ('queued', 'running'):
                        job.state, job.stage = 'failed', '来源已删除，任务停止'
                        job.error = {'code': 'SOURCE_REMOVED', 'message': '引用素材已删除。'}
            repo.audit(db, auth['user_id'], 'asset_delete_requested', asset.id,
                       affected_outputs=sum(bool(v.output_path) for v in dependent_versions))
        cleanup_deleted_files(repo, asset_id=asset_id, owner_id=auth['user_id'])
        cleanup_collaboration(repo, owner_id=auth['user_id'], episode_id=asset.episode_id)
        # A different user's pending deletion must not make this one fail; nor
        # may a removed source hide a remaining derived/context/paid audio file.
        with repo.factory() as db:
            pending_files = int(asset_cleanup_pending(db, repo, db.get(Asset, asset_id)))
        with repo.transaction() as db:
            repo.audit(db, auth['user_id'], 'asset_cleanup_pending' if pending_files else 'asset_deleted',
                       asset_id, pending_files=pending_files)
        if pending_files:
            raise DomainError('DELETION_PENDING',
                              '访问已停止，但本地文件清理尚未完成；可重试删除或重启服务继续清理。', 503)
        return Ok()

    def require_cloud(repo):
        Pipeline(repo, model_services).require_ready()

    @app.post('/api/v1/episodes/{episode_id}/director/messages', response_model=DirectorState)
    def director_message(episode_id: str, body: DirectorMessageIn,
                         auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            return director_guidance.record(repo, db, episode, **body.model_dump())

    def workflow_service(repo, episode, kind, version=None):
        service, marker = workflow_access.resolve(model_services, repo, episode.id,
            episode.owner_id, kind, version.id if version else None)
        Pipeline(repo, service).require_ready()
        return service, marker

    def enqueue_ai(db, repo, episode, kind, version=None, workflow_grant=None, model_profile=None):
        unknown = db.scalars(select(Budget).join(ModelCall, ModelCall.reservation_id == Budget.id).where(
            ModelCall.episode_id == episode.id, ModelCall.owner_id == episode.owner_id,
            (Budget.state.in_(('unknown', 'reserved'))) | ((Budget.state == 'settled') & Budget.cost.is_(None)))).first()
        if unknown:
            # Active duplicate clicks still return the existing task below.
            linked = db.scalars(select(Job).where(Job.episode_id == episode.id, Job.kind == kind,
                Job.state.in_(('queued', 'running')))).first()
            if linked and (not version or linked.version_id == version.id):
                if kind == 'directions':
                    director_guidance.assert_revision(db, episode, linked.payload.get('brief_revision', 0))
                return linked
            raise DomainError('COST_UNKNOWN', '本期尚有未结清的费用预留，核对完成前暂停新模型调用。', 409)
        active = db.scalars(select(Job).where(Job.episode_id == episode.id, Job.owner_id == episode.owner_id,
            Job.kind == kind, Job.state.in_(('queued', 'running')))).first()
        if active:
            if version and active.version_id != version.id:
                raise DomainError('JOB_ACTIVE', '本期已有同类任务处理中，请稍后重试。', 409)
            if kind == 'directions':
                director_guidance.assert_revision(db, episode, active.payload.get('brief_revision', 0))
            return active
        assets = active_assets(db, episode)
        if not assets:
            raise DomainError('ASSETS_REQUIRED', '请先上传视频素材。', 409)
        if kind != 'analysis':
            assert_sources(db, episode, episode.analysis_asset_ids)
        job = Job(owner_id=episode.owner_id, episode_id=episode.id, version_id=version.id if version else None,
            kind=kind, idempotency_key=kind + ':' + uid(), payload={
                'asset_ids': [a.id for a in assets], 'revision': episode.evidence_revision, 'steps': {}})
        if workflow_grant:
            job.payload = {**job.payload, 'workflow_grant': workflow_grant, 'model_profile': model_profile}
            if kind == 'analysis':
                job.idempotency_key = workflow_grant['scope'] + ':analysis'
        if kind == 'directions':
            job.payload = {**job.payload, 'brief_revision': director_guidance.revision(db, episode)}
        db.add(job)
        db.flush()
        repo.audit(db, episode.owner_id, kind + '_queued', job.id)
        return job

    @app.get('/api/v1/episodes/{episode_id}/director/chat', response_model=DirectorChatState)
    def director_chat_state(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.factory() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            return director_chat.state(repo, db, episode, model_services)

    @app.post('/api/v1/episodes/{episode_id}/director/chat', response_model=JobOut)
    def send_director_chat(episode_id: str, body: DirectorChatIn,
                           auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            return job_out(director_chat.enqueue(repo, db, episode, model_services, **body.model_dump()))

    @app.post('/api/v1/episodes/{episode_id}/director/actions/{proposal_id}', response_model=DirectorActionOut)
    def apply_director_action(episode_id: str, proposal_id: str, body: DirectorActionIn,
                              auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            return director_chat.apply(repo, db, episode, proposal_id, model_services,
                                       enqueue_ai, confirm=body.confirm)

    @app.post('/api/v1/episodes/{episode_id}/analysis', response_model=JobOut)
    def analyze(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            service, marker = workflow_service(repo, episode, 'analysis')
            if marker:
                previous = db.scalars(select(Job).where(Job.owner_id == episode.owner_id,
                    Job.episode_id == episode.id, Job.idempotency_key == marker['scope'] + ':analysis')).first()
                if previous:
                    if previous.state == 'succeeded':
                        if (not episode.evidence or episode.analysis_state not in ('review', 'confirmed')
                                or previous.payload.get('model_profile', {}).get('grant_digest') != marker['grant_digest']
                                or set(episode.analysis_asset_ids) != {a['id'] for a in service.grant['assets']}):
                            raise DomainError('SCOPE_CHANGED', '已有分析与当前授权不符，请先核对。', 409)
                    elif previous.payload.get('workflow_grant') != marker:
                        raise DomainError('ANALYSIS_RETRY_REQUIRED', '已有分析需从原检查点恢复，请先核对任务。', 409)
                    return job_out(previous)
            job = enqueue_ai(db, repo, episode, 'analysis', workflow_grant=marker, model_profile=service.profile() if marker else None)
            return job_out(job)

    @app.put('/api/v1/episodes/{episode_id}/evidence', response_model=EpisodeOut)
    def review_evidence(episode_id: str, body: EvidenceReview, auth=Depends(session), repo=Depends(repository)):
        if not body.confirmed:
            raise DomainError('CONFIRM_REQUIRED', '请确认本期内容后继续。', 409)
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            if episode.evidence_revision != body.revision:
                raise DomainError('STALE_REVISION', '证据已被更新，请刷新后重新核对。', 409)
            if episode.analysis_state not in ('review', 'confirmed'):
                raise DomainError('ANALYSIS_REQUIRED', '请先完成当前素材分析。', 409)
            assets = {a.id: a for a in assert_sources(db, episode, episode.analysis_asset_ids)}
            original = {row['id']: row for row in episode.evidence}
            seen = set()
            for item in body.items:
                source = original.get(item.id)
                if not source or item.id in seen or item.asset_id != source['asset_id'] or item.kind != source['kind']:
                    raise DomainError('INVALID_EVIDENCE', '证据标识、来源或类别不匹配。', 422)
                if item.asset_id not in assets or item.end > assets[item.asset_id].duration:
                    raise DomainError('INVALID_EVIDENCE', '证据区间超出原片。', 422)
                seen.add(item.id)
            invalidate_analysis(repo, db, episode)
            episode.evidence = [row.model_dump() for row in body.items]
            episode.evidence_confirmed_revision = episode.evidence_revision
            episode.analysis_state = 'confirmed'
            repo.audit(db, auth['user_id'], 'evidence_confirmed', episode.id,
                       revision=episode.evidence_revision, confirmation_mode=body.confirmation_mode)
            return episode_out(db, repo, episode)

    @app.post('/api/v1/episodes/{episode_id}/directions', response_model=JobOut)
    def propose(episode_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            service, marker = workflow_service(repo, episode, 'directions')
            if episode.evidence_confirmed_revision != episode.evidence_revision or not episode.evidence:
                raise DomainError('REVIEW_REQUIRED', '请先核对至少一条可靠证据。', 409)
            director_guidance.compiled_background(db, episode)
            return job_out(enqueue_ai(db, repo, episode, 'directions', workflow_grant=marker, model_profile=service.profile() if marker else None))

    @app.post('/api/v1/episodes/{episode_id}/direction', response_model=VersionOut, status_code=201)
    def select_direction(episode_id: str, body: DirectionSelect, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            episode = repo.owned(db, Episode, episode_id, auth['user_id'])
            auto_key = None
            if body.auto_render_original:
                if not body.client_id:
                    raise DomainError('REQUEST_ID_REQUIRED', '请刷新后重新选择拍法。', 422)
                if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
                    raise DomainError('MEDIA_UNAVAILABLE', '请先安装 FFmpeg。', 503)
                auto_key = 'director-original-draft:' + episode.id + ':' + body.client_id
            if (episode.evidence_revision != body.revision or episode.directions_revision != body.revision
                    or episode.evidence_confirmed_revision != body.revision):
                raise DomainError('STALE_REVISION', '提案依据已更新，请重新核对并取得方向。', 409)
            direction = next((d for d in episode.directions if d['id'] == body.direction_id), None)
            if not direction:
                raise DomainError('NOT_FOUND', '此方向不存在。', 404)
            requested_brief = body.brief_revision
            if requested_brief is None and director_guidance.revision(db, episode) == 0:
                requested_brief = 0  # Older clients without a guidance session.
            director_guidance.assert_revision(db, episode, requested_brief)
            if not director_guidance.directions_current(db, episode):
                raise DomainError('DIRECTOR_BRIEF_CHANGED', '导演需求已更新，请重新生成方向；已有成片不受影响。', 409)
            if auto_key:
                existing = db.scalars(select(Job).where(Job.owner_id == auth['user_id'], Job.idempotency_key == auto_key)).first()
                if existing:
                    if existing.payload.get('delegation') != body.model_dump():
                        raise DomainError('REQUEST_CONFLICT', '这个请求已用于另一种拍法，请刷新后重试。', 409)
                    saved = repo.owned(db, Version, existing.version_id, auth['user_id'])
                    source_segments(repo, db, saved)
                    return version_out(saved)
            assets = {a.id: a for a in assert_sources(db, episode, episode.analysis_asset_ids)}
            from .services.source_windows import planning_sources
            available_sources = planning_sources(episode.evidence)
            for span in direction['evidence']:
                if not any(row['asset_id'] == span['asset_id'] and row['start'] <= span['start'] < span['end'] <= row['end'] for row in available_sources):
                    raise DomainError('INVALID_PLAN', '方向包含未经核对的区间。', 409)
            segments, narration = build_edit_plan(direction, assets, body.packaging)
            guarded = any(s.get('evidence_ids') or s.get('purpose') for s in segments)
            if guarded:
                check_edit(segments, episode)
            version = Version(owner_id=auth['user_id'], episode_id=episode.id, title=direction['title'],
                origin='ai' if model_services.is_real else 'demo', segments=segments, direction_id=direction['id'], evidence_revision=body.revision,
                narration_text='' if body.auto_render_original else narration, packaging=body.packaging)
            db.add(version)
            db.flush()
            if guarded:
                mark_edit(repo, db, version)
            episode.selected_direction = direction['id']
            if auto_key:
                source_segments(repo, db, version)
                job = Job(owner_id=auth['user_id'], episode_id=episode.id, version_id=version.id,
                    kind='render', idempotency_key=auto_key,
                    payload={'delegation': body.model_dump(), 'stop_at': 'preview', 'cloud_calls_allowed': False},
                    stage='导演已安排原声初稿，等待本机合成')
                db.add(job); db.flush()
                version.job_id, version.state = job.id, 'rendering'
                from .services.director_run import create as create_run
                create_run(repo, db, version, job, requested_brief)
                repo.audit(db, auth['user_id'], 'director_original_draft_queued', job.id)
            repo.audit(db, auth['user_id'], 'direction_selected', version.id, revision=body.revision)
            return version_out(version)

    @app.post('/api/v1/versions/{version_id}/narration', response_model=JobOut)
    def narrate(version_id: str, body: NarrationConfirm, auth=Depends(session), repo=Depends(repository)):
        if not body.confirm_text:
            raise DomainError('CONFIRM_REQUIRED', '请先核对并明确确认此版旁白文案。', 409)
        with repo.transaction() as db:
            version = repo.owned(db, Version, version_id, auth['user_id'])
            episode = db.get(Episode, version.episode_id)
            service, marker = workflow_service(repo, episode, 'narration', version)
            if version.state == 'restricted':
                raise DomainError('VERSION_UNAVAILABLE', '版本已受限，请核对来源后保存新版本。', 409)
            if version.narration_path and version.narration_approved:
                source_segments(repo, db, version)
                if not repo.path(version.narration_path).is_file():
                    raise DomainError('NARRATION_MISSING', '已生成的旁白文件缺失，请保存新版本。', 409)
                previous = db.scalars(select(Job).where(Job.version_id == version.id, Job.kind == 'narration',
                    Job.owner_id == auth['user_id'], Job.state == 'succeeded').order_by(Job.created_at.desc())).first()
                if previous:
                    return job_out(previous)
                raise DomainError('NARRATION_READY', '本版已复用确认过的旁白，可直接生成预览，无需再次调用模型。', 409)
            if version.state not in ('draft', 'failed', 'voicing') or not version.narration_text:
                raise DomainError('VERSION_UNAVAILABLE', '此版本不能生成旁白；修改内容请保存新版本。', 409)
            source_segments(repo, db, version)
            episode = db.get(Episode, version.episode_id)
            # Manual versions may use user-written narration without fabricated evidence.
            if version.evidence_revision is None:
                if episode.analysis_asset_ids != [a.id for a in active_assets(db, episode)]:
                    raise DomainError('ANALYSIS_REQUIRED', '请先分析并核对来源，再使用云旁白；本地手动剪接不受影响。', 409)
            version.narration_approved = True
            job = enqueue_ai(db, repo, episode, 'narration', version, workflow_grant=marker, model_profile=service.profile() if marker else None)
            version.job_id, version.state, version.error = job.id, 'voicing', None
            return job_out(job)

    @app.post('/api/v1/jobs/{job_id}/retry', response_model=JobOut)
    def retry_job(job_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            job = repo.owned(db, Job, job_id, auth['user_id'])
            if job.kind not in ('analysis', 'directions', 'narration', 'director_chat', 'director_voice') or job.state != 'failed':
                raise DomainError('RETRY_UNAVAILABLE', '此任务不能在此处重试。', 409)
            if job.payload.get('workflow_grant'):
                service, _ = workflow_access.resolve(model_services, repo, job.episode_id,
                    job.owner_id, job.kind, job.version_id, bound=job.payload['workflow_grant'])
                Pipeline(repo, service).require_ready()
            elif job.kind == 'director_chat':
                turn = repo.owned(db, DirectorChatTurn, job.payload.get('turn_id'), auth['user_id'])
                director_chat.require_ready(model_services, turn.context)
            elif job.kind == 'director_voice':
                Pipeline(repo, model_services).require_ready('director_voice')
            elif job.kind == 'directions' and job.payload.get('chat_proposal_id'):
                context = director_chat.scoped_direction_context(repo, db, job)
                Pipeline(repo, model_services).require_ready('directions', context)
            else:
                require_cloud(repo)
            Pipeline(repo, model_services)._validate_job(db, job)
            for call in db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)):
                budget = db.get(Budget, call.reservation_id)
                if budget.state in ('unknown', 'reserved') or (budget.state == 'settled' and budget.cost is None):
                    raise DomainError('COST_UNKNOWN', '上次调用费用未核实，已保留预算；核对账单前不能重试。', 409)
            competing = db.scalars(select(Job).where(Job.episode_id == job.episode_id, Job.kind == job.kind,
                Job.state.in_(('queued','running')))).first()
            if competing:
                return job_out(competing)
            job.state, job.stage, job.error = 'queued', '等待从已保存检查点继续', None
            if job.version_id:
                version = db.get(Version, job.version_id)
                version.state = 'voicing'
            repo.audit(db, auth['user_id'], 'job_retry_requested', job.id)
            return job_out(job)

    register_collaboration(app, session, repository, settings, cookie_name)
    register_sharing(app, session, repository, settings, cookie_name)
    register_director_voice(app, session, repository, model_services)
    app.add_middleware(BodyLimitMiddleware, upload_bytes=settings.max_upload_bytes)
    return app


def production_app():
    """Uvicorn --factory entry; importing create_app never reads credentials."""
    return create_app()
