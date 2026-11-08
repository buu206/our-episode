"""Owner and invitation-holder APIs, with separate cookies and CSRF scopes."""
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from uuid import UUID

from fastapi import Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from .collaboration_models import (Adoption, ExpressionRevision, Invitation, Objection,
                                   Participant, ParticipantSession, Submission)
from .collaboration_schemas import (ConfirmIn, SubmissionConfirmIn, DraftIn, InvitationIn, ObjectionIn,
                                    ParticipantLogin, QuestionsIn, RevisionDecision,
                                    RevisionIn, TextSubmissionIn, ParticipantAuthOut, InvitationOut,
                                    InvitationCreatedOut, InvitationsOut, RevisionOut, SubmissionOut,
                                    SubmissionsOut, ObjectionOut, ObjectionsOut, literal_text)
from .models import Asset, Episode, Job, now, uid
from .repository import DomainError
from .schemas import JobOut, Ok
from .services.artifacts import artifact_workspace
from .services import collaboration as service
from .services.media import MediaError


def _job_out(job):
    return {"id": job.id, "kind": job.kind, "state": job.state, "stage": job.stage,
            "error": job.error, "version_id": job.version_id, "result": job.result}


def _invite_out(db, invitation, *, participant=False):
    job = db.get(Job, invitation.context_job_id) if invitation.context_job_id else None
    ready = bool(invitation.context_path)
    try:
        if invitation.context:
            service.context_source(db, invitation)
    except DomainError:
        ready = False
    return {"id": invitation.id, "role": invitation.role, "label": invitation.label,
            "expires_at": invitation.expires_at, "closed": invitation.closed_at is not None,
            "accepting_new": service.accepting_new(invitation), "context": invitation.context,
            "questions": invitation.questions, "context_job_id": invitation.context_job_id,
            "context_error": job.error if job else None,
            "context_state": "ready" if ready else (job.state if job else "none"),
            "context_preview_url": "/api/v1/participant/context/media" if participant and ready else None,
            "created_at": invitation.created_at}


def _revision_out(revision):
    return {key: getattr(revision, key) for key in ("id", "submission_id", "text", "start", "end", "state", "created_at")}


def _submission_out(db, submission, *, participant=False):
    person = db.get(Participant, submission.participant_id)
    job = db.get(Job, submission.preview_job_id) if submission.preview_job_id else None
    can_view = participant or submission.state == "submitted"
    ready = bool(submission.preview_path) and submission.deleted_at is None
    prefix = "/api/v1/participant" if participant else "/api/v1/collaboration"
    revisions = list(db.scalars(select(ExpressionRevision).where(ExpressionRevision.submission_id == submission.id)
                               .order_by(ExpressionRevision.created_at)))
    adoptions = []
    for adoption in db.scalars(select(Adoption).where(Adoption.submission_id == submission.id)):
        item = {key: getattr(adoption, key) for key in ("id", "revision_id", "state", "asset_id", "job_id")}
        task = db.get(Job, adoption.job_id) if adoption.job_id else None
        if item["state"] == "pending" and task and task.state == "failed":
            item["state"] = "failed"
        item["error"] = task.error if task else None
        adoptions.append(item)
    deleted = submission.deleted_at is not None and submission.state == "withdrawn"
    cleanup_jobs = [db.get(Job, job_id) for job_id in
                    {submission.upload_job_id, submission.preview_job_id} - {None}]
    deletion_pending = deleted and bool(submission.source_path or submission.preview_path or any(
        task and task.payload.get("artifacts") for task in cleanup_jobs))
    can_delete = (not revisions and not adoptions and submission.state in ("draft", "failed", "withdrawn")
                  and (not deleted or deletion_pending))
    return {"id": submission.id, "invite_id": submission.invite_id, "participant_id": submission.participant_id,
            "display_name": person.display_name, "kind": submission.kind, "state": submission.state,
            "text": submission.text if can_view else "", "question_id": submission.question_id,
            "filename": submission.filename, "duration": submission.duration, "size": submission.size,
            "preview_state": "ready" if ready or submission.kind == "text" else (job.state if job else "none"),
            "preview_job_id": submission.preview_job_id,
            "preview_error": job.error if job else None,
            "media_url": f"{prefix}/submissions/{submission.id}/media" if ready and can_view else None,
            "created_at": submission.created_at,
            "revisions": [_revision_out(r) for r in revisions] if can_view else [],
            "adoptions": adoptions, "can_delete_draft": can_delete,
            "deleted": deleted, "deletion_pending": deletion_pending}


def _objection_out(db, objection):
    return {**{key: getattr(objection, key) for key in ("id", "invite_id", "participant_id", "submission_id",
              "asset_id", "start", "end", "reason", "state", "created_at", "resolved_at")},
            "display_name": db.get(Participant, objection.participant_id).display_name}


def _request_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DomainError("INVALID_REQUEST_ID", "请为本次投稿生成独立请求编号。", 422) from None


def _question(invitation, question_id):
    if question_id and not any(q["id"] == question_id for q in invitation.questions):
        raise DomainError("INVALID_QUESTION", "问题不属于本人邀请。", 422)


def _input_hash(kind, text, question_id, filename, content_hash=""):
    return service.digest(json.dumps([kind, text, question_id, filename, content_hash], ensure_ascii=False))


def register_collaboration(app, owner_session, repository, settings, cookie_name):
    participant_cookie = cookie_name + "_participant"

    def participant_auth(request: Request, repo=Depends(repository)):
        token = request.cookies.get(participant_cookie, "")
        with repo.factory() as db:
            row = db.get(ParticipantSession, service.digest(token)) if token else None
            person = db.get(Participant, row.participant_id) if row else None
            invitation = db.get(Invitation, person.invite_id) if person else None
            if not row or row.expires_at <= time.time() or not person or not invitation:
                raise DomainError("PARTICIPANT_AUTH_REQUIRED", "请使用本次邀请登录。", 401)
            if request.method not in ("GET", "HEAD") and not hmac.compare_digest(
                    row.csrf, request.headers.get("x-csrf-token", "")):
                raise DomainError("CSRF_FAILED", "参与会话校验失败，请刷新后重试。", 403)
            return {"participant_id": person.id, "invite_id": invitation.id, "display_name": person.display_name,
                    "csrf_token": row.csrf, "token_hash": row.token_hash}

    def public_auth(auth):
        return {key: auth[key] for key in ("participant_id", "invite_id", "display_name", "csrf_token")}

    def owner_submission(db, repo, submission_id, owner):
        row = repo.owned(db, Submission, submission_id, owner)
        ever_submitted = db.scalars(select(ExpressionRevision.id).where(ExpressionRevision.submission_id == row.id)).first()
        if row.state not in ("submitted", "withdrawn") or not ever_submitted:
            raise DomainError("NOT_FOUND", "内容尚未由本人提交。", 404)
        return row

    def new_content(db, auth):
        invitation = db.get(Invitation, auth["invite_id"])
        service.require_new(invitation)
        if invitation.role == "review":
            raise DomainError("ROLE_FORBIDDEN", "此邀请只允许核对授权片段和提出异议。", 403)
        return invitation

    @app.post("/api/v1/episodes/{episode_id}/collaboration/invitations", status_code=201, response_model=InvitationCreatedOut)
    def create_invitation(episode_id: str, body: InvitationIn, auth=Depends(owner_session), repo=Depends(repository)):
        code = secrets.token_urlsafe(24)
        with repo.transaction() as db:
            repo.owned(db, Episode, episode_id, auth["user_id"])
            invitation = Invitation(owner_id=auth["user_id"], episode_id=episode_id, role=body.role,
                                    label=body.label.strip(), code_hash=service.digest(code),
                                    expires_at=time.time() + 7 * 86400,
                                    context=body.context.model_dump() if body.context else None,
                                    questions=[{"id": uid(), "text": q, "skipped": False} for q in body.questions])
            if not invitation.label:
                raise DomainError("INVALID_REQUEST", "称呼不能为空。", 422)
            if invitation.context:
                asset = service.context_source(db, invitation)
                service.guard_asset(db, asset, {**invitation.context, "_context": True})
            db.add(invitation)
            db.flush()
            if invitation.context:
                job = Job(owner_id=invitation.owner_id, episode_id=episode_id, kind="context_preview",
                          idempotency_key="context:" + invitation.id,
                          payload={"invite_id": invitation.id, "asset_ids": [invitation.context["asset_id"]]})
                db.add(job)
                db.flush()
                invitation.context_job_id = job.id
            repo.audit(db, auth["user_id"], "invitation_created", invitation.id, role=body.role)
            return {**_invite_out(db, invitation), "invite_code": code}

    @app.get("/api/v1/episodes/{episode_id}/collaboration/invitations", response_model=InvitationsOut)
    def list_invitations(episode_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth["user_id"])
            return {"items": [_invite_out(db, row) for row in db.scalars(select(Invitation).where(
                    Invitation.owner_id == auth["user_id"], Invitation.episode_id == episode_id).order_by(Invitation.created_at))]}

    @app.post("/api/v1/collaboration/invitations/{invitation_id}/close", response_model=InvitationOut)
    def close_invitation(invitation_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.transaction() as db:
            row = repo.owned(db, Invitation, invitation_id, auth["user_id"])
            row.closed_at = row.closed_at or time.time()
            repo.audit(db, auth["user_id"], "invitation_closed", row.id)
            return _invite_out(db, row)

    @app.post("/api/v1/collaboration/invitations/{invitation_id}/retry", response_model=JobOut)
    def retry_context(invitation_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.transaction() as db:
            invitation = repo.owned(db, Invitation, invitation_id, auth["user_id"])
            service.context_source(db, invitation)
            job = db.get(Job, invitation.context_job_id) if invitation.context_job_id else None
            if not job:
                raise DomainError("NOT_FOUND", "没有可重试的上下文任务。", 404)
            if job.state == "failed":
                job.state, job.stage, job.error = "queued", "等待重新准备授权片段", None
            return _job_out(job)

    @app.post("/api/v1/participant/auth/login", response_model=ParticipantAuthOut)
    def participant_login(body: ParticipantLogin, response: Response, repo=Depends(repository)):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with repo.transaction() as db:
            invitation = db.scalars(select(Invitation).where(Invitation.code_hash == service.digest(body.invite_code))).first()
            if not invitation:
                raise DomainError("INVALID_INVITATION", "邀请无效。", 401)
            person = db.scalars(select(Participant).where(Participant.invite_id == invitation.id)).first()
            if person is None:
                service.require_new(invitation)
                if not body.display_name.strip():
                    raise DomainError("INVALID_REQUEST", "请填写本人希望展示的称呼。", 422)
                person = Participant(invite_id=invitation.id, display_name=body.display_name.strip())
                db.add(person)
                db.flush()
            # A closed code cannot claim a new identity, but an existing holder
            # retains access to consent, withdrawal and objection management.
            db.add(ParticipantSession(token_hash=service.digest(token), participant_id=person.id,
                                      csrf=csrf, expires_at=time.time() + settings.session_seconds))
            result = {"participant_id": person.id, "invite_id": invitation.id,
                      "display_name": person.display_name, "csrf_token": csrf}
        response.set_cookie(participant_cookie, token, max_age=settings.session_seconds,
                            httponly=True, samesite="strict", path="/")
        return result

    @app.get("/api/v1/participant/auth/me", response_model=ParticipantAuthOut)
    def participant_me(auth=Depends(participant_auth)):
        return public_auth(auth)

    @app.post("/api/v1/participant/auth/logout", response_model=Ok)
    def participant_logout(response: Response, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            db.delete(db.get(ParticipantSession, auth["token_hash"]))
        response.delete_cookie(participant_cookie, path="/")
        return {"ok": True}

    @app.get("/api/v1/participant/invitation", response_model=InvitationOut)
    def participant_invitation(auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.factory() as db:
            return _invite_out(db, db.get(Invitation, auth["invite_id"]), participant=True)

    @app.put("/api/v1/participant/invitation/questions", response_model=InvitationOut)
    def edit_questions(body: QuestionsIn, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            invitation = new_content(db, auth)
            old_ids = {q["id"] for q in invitation.questions}
            new_ids = {q.id for q in body.questions}
            if old_ids != new_ids or len(new_ids) != len(body.questions) or any(not q.text.strip() for q in body.questions):
                raise DomainError("INVALID_QUESTION", "只能修改或跳过本人已收到的问题。", 422)
            invitation.questions = [q.model_dump() for q in body.questions]
            return _invite_out(db, invitation, participant=True)

    @app.get("/api/v1/participant/context/media")
    def participant_context(auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.factory() as db:
            invitation = db.get(Invitation, auth["invite_id"])
            service.context_source(db, invitation)
            if not invitation.context_path:
                raise DomainError("CONTEXT_NOT_READY", "授权片段正在本机准备，请稍后重试。", 409)
            return FileResponse(service.controlled_path(repo, invitation.context_path, read=True), media_type="video/mp4")

    @app.get("/api/v1/participant/submissions", response_model=SubmissionsOut)
    def participant_submissions(auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.factory() as db:
            return {"items": [_submission_out(db, row, participant=True) for row in db.scalars(
                select(Submission).where(Submission.participant_id == auth["participant_id"]).order_by(Submission.created_at))]}

    @app.post("/api/v1/participant/submissions", status_code=201, response_model=SubmissionOut)
    def text_submission(body: TextSubmissionIn, auth=Depends(participant_auth), repo=Depends(repository)):
        request_id = _request_id(body.client_request_id)
        fingerprint = _input_hash("text", body.text, body.question_id, None)
        with repo.transaction() as db:
            existing = db.scalars(select(Submission).where(Submission.participant_id == auth["participant_id"],
                                                          Submission.client_request_id == request_id)).first()
            if existing:
                if existing.request_hash != fingerprint:
                    raise DomainError("IDEMPOTENCY_CONFLICT", "该请求编号已用于不同投稿。", 409)
                return _submission_out(db, existing, participant=True)
            invitation = new_content(db, auth)
            _question(invitation, body.question_id)
            row = Submission(owner_id=invitation.owner_id, episode_id=invitation.episode_id, invite_id=invitation.id,
                             participant_id=auth["participant_id"], client_request_id=request_id,
                             request_hash=fingerprint, kind="text", text=body.text, question_id=body.question_id)
            db.add(row)
            db.flush()
            return _submission_out(db, row, participant=True)

    @app.post("/api/v1/participant/submissions/upload", status_code=201, response_model=SubmissionOut)
    async def upload_submission(request: Request, file: UploadFile = File(...), kind: str = Form(...),
                                client_request_id: str = Form(...), text: str = Form(""), question_id: str | None = Form(None),
                                auth=Depends(participant_auth), repo=Depends(repository)):
        row_id = job_id = target = None
        succeeded = False
        target_created = False
        try:
            request_id = _request_id(client_request_id)
            name = re.sub(r"[\x00-\x1f\x7f]", "", (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1])[:200]
            suffix = Path(name).suffix.lower()
            allowed = {"audio": (".wav", ".mp3", ".m4a"), "video": (".mp4", ".mov")}
            if kind not in allowed or suffix not in allowed[kind] or len(text) > 100:
                raise DomainError("INVALID_FORMAT", "采访支持 WAV/MP3/M4A 语音或 MP4/MOV 视频；表达最多100字。", 422)
            try:
                literal_text(text)
            except ValueError:
                raise DomainError("INVALID_REQUEST", "表达不能包含换行或隐藏控制字符，请整理为一句说明。", 422) from None
            with repo.transaction() as db:
                existing = db.scalars(select(Submission).where(Submission.participant_id == auth["participant_id"],
                                                              Submission.client_request_id == request_id)).first()
                existing_id = existing.id if existing else None
                if existing and existing.state in ("uploading", "failed"):
                    raise DomainError("UPLOAD_INACTIVE", "原请求仍在上传或已失败；请刷新状态后对失败投稿重新上传。", 409)
                if existing:
                    invitation = db.get(Invitation, existing.invite_id)
                    role = invitation.role
                if not existing:
                    invitation = new_content(db, auth)
                    if invitation.role == "contribute" and kind != "video":
                        raise DomainError("INVALID_FORMAT", "供材邀请接收现场视频；语音请使用采访邀请。", 422)
                    _question(invitation, question_id)
                    service.check_quota(db, invitation.episode_id, settings, count=1)
                    source_path = "collaboration/" + uid() + suffix
                    row = Submission(owner_id=invitation.owner_id, episode_id=invitation.episode_id,
                                     invite_id=invitation.id, participant_id=auth["participant_id"],
                                     client_request_id=request_id, kind=kind, state="uploading", text=text,
                                     question_id=question_id, filename=name, source_path=source_path)
                    db.add(row)
                    db.flush()
                    upload_job = Job(owner_id=invitation.owner_id, episode_id=invitation.episode_id,
                                     kind="collaboration_upload", state="running", idempotency_key="upload:" + row.id,
                                     payload={"submission_id": row.id})
                    db.add(upload_job)
                    db.flush()
                    row.upload_job_id = upload_job.id
                    row_id, job_id = row.id, upload_job.id
                    role = invitation.role
                    target = service.collaboration_path(repo, source_path)
                max_bytes, max_seconds = service.submission_limits(role, settings)
                size_message = ("单段采访最多100 MB。" if role == "interview"
                                else "普通供材不能超过本期1 GB总额度。")
            async with request.app.state.upload_lock:
                if existing_id:
                    content_hash, size = hashlib.sha256(), 0
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            raise DomainError("UPLOAD_TOO_LARGE", size_message, 413)
                        content_hash.update(chunk)
                    fingerprint = _input_hash(kind, text, question_id, name, content_hash.hexdigest())
                    with repo.factory() as db:
                        existing = service.owned_participant(db, auth["participant_id"], Submission, existing_id)
                        if not hmac.compare_digest(existing.request_hash or "", fingerprint):
                            raise DomainError("IDEMPOTENCY_CONFLICT", "同一请求编号不能提交不同文件或内容。", 409)
                        return _submission_out(db, existing, participant=True)
                with artifact_workspace(repo, job_id) as workspace:
                    temp = workspace / ("source" + suffix)
                    size, content_hash = 0, hashlib.sha256()
                    with temp.open("xb") as output:
                        while chunk := await file.read(1024 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise DomainError("UPLOAD_TOO_LARGE", size_message, 413)
                            content_hash.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if role == "interview":
                        from .services.interview_media import probe_interview
                        info = await asyncio.to_thread(probe_interview, temp, kind)
                    else:
                        from .services.media import probe
                        info = await asyncio.to_thread(probe, temp)
                    if info["duration"] > max_seconds:
                        message = "单段采访最多60秒。" if role == "interview" else "普通供材不能超过本期10分钟总额度。"
                        raise DomainError("DURATION_EXCEEDED", message, 422)
                    fingerprint = _input_hash(kind, text, question_id, name, content_hash.hexdigest())
                    with repo.transaction() as db:
                        invitation = new_content(db, auth)
                        row = service.owned_participant(db, auth["participant_id"], Submission, row_id)
                        if row.state != "uploading" or row.deleted_at is not None:
                            raise DomainError("SUBMISSION_UNAVAILABLE", "上传任务已失效。", 409)
                        service.check_quota(db, row.episode_id, settings, size=size, seconds=info["duration"])
                        os.link(temp, target)
                        target_created = True
                        row.state, row.size, row.duration, row.request_hash = "draft", size, info["duration"], fingerprint
                        upload_job = db.get(Job, job_id)
                        upload_job.state, upload_job.stage = "succeeded", "投稿草稿已保存，等待本人确认"
                        preview_job = Job(owner_id=row.owner_id, episode_id=row.episode_id, kind="submission_preview",
                                          idempotency_key="submission-preview:" + row.id, payload={"submission_id": row.id})
                        db.add(preview_job)
                        db.flush()
                        row.preview_job_id = preview_job.id
                        result = _submission_out(db, row, participant=True)
                    succeeded = True
                    return result
        except MediaError as exc:
            raise DomainError(exc.code, exc.message, 422) from None
        finally:
            if row_id and not succeeded:
                with repo.transaction() as db:
                    row = db.get(Submission, row_id)
                    row.state, row.deleted_at = "failed", time.time()
                    if not target_created:
                        row.source_path = None
                    elif target and target.exists():
                        row.size = target.stat().st_size
                        row.duration = info["duration"]
                    job = db.get(Job, job_id)
                    job.state, job.error = "failed", {"code": "UPLOAD_FAILED", "message": "投稿未完成，可重新上传。"}
                if target and target_created:
                    try:
                        target.unlink(missing_ok=True)
                    except OSError:
                        pass  # The durable deleted row owns a cleanup retry.
                    else:
                        with repo.transaction() as db:
                            db.get(Submission, row_id).source_path = None
            await file.close()

    @app.put("/api/v1/participant/submissions/{submission_id}", response_model=SubmissionOut)
    def edit_draft(submission_id: str, body: DraftIn, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            invitation = new_content(db, auth)
            row = service.owned_participant(db, auth["participant_id"], Submission, submission_id)
            if row.state != "draft":
                raise DomainError("DRAFT_REQUIRED", "已提交内容不可覆写；请使用独立表达修订。", 409)
            _question(invitation, body.question_id)
            row.text, row.question_id = body.text, body.question_id
            return _submission_out(db, row, participant=True)

    @app.post("/api/v1/participant/submissions/{submission_id}/retry", response_model=JobOut)
    def retry_preview(submission_id: str, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            row = service.owned_participant(db, auth["participant_id"], Submission, submission_id)
            if row.state != "draft" or row.deleted_at is not None:
                raise DomainError("DRAFT_REQUIRED", "只有本人的未提交草稿可重试预览。", 409)
            service.collaboration_path(repo, row.source_path, read=True)
            job = db.get(Job, row.preview_job_id) if row.preview_job_id else None
            if not job:
                raise DomainError("NOT_FOUND", "没有可重试的投稿预览任务。", 404)
            if job.state == "failed":
                job.state, job.stage, job.error = "queued", "等待重新准备本人预览", None
            return _job_out(job)

    @app.get("/api/v1/participant/submissions/{submission_id}/media")
    def participant_media(submission_id: str, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.factory() as db:
            row = service.owned_participant(db, auth["participant_id"], Submission, submission_id)
            if row.deleted_at is not None or not row.preview_path:
                raise DomainError("PREVIEW_NOT_READY", "投稿预览未就绪或已删除。", 409)
            return FileResponse(service.controlled_path(repo, row.preview_path, read=True), media_type="video/mp4")

    @app.post("/api/v1/participant/submissions/{submission_id}/submit", response_model=SubmissionOut)
    def submit(submission_id: str, body: SubmissionConfirmIn, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            row = service.owned_participant(db, auth["participant_id"], Submission, submission_id)
            if body.expected_draft is not None and (
                    row.text != body.expected_draft.text or row.question_id != body.expected_draft.question_id):
                raise DomainError("DRAFT_CHANGED", "草稿已被修改，请重新核对当前文字和对应问题后再确认提交。", 409)
            if row.state == "submitted":
                return _submission_out(db, row, participant=True)
            new_content(db, auth)
            if row.state != "draft" or row.deleted_at is not None:
                raise DomainError("DRAFT_REQUIRED", "只有本人草稿可以提交。", 409)
            if row.kind != "text":
                if not row.preview_path:
                    raise DomainError("PREVIEW_NOT_READY", "请等待本机预览就绪后核对本人内容。", 409)
                service.controlled_path(repo, row.preview_path, read=True)
            elif not row.text.strip():
                raise DomainError("EMPTY_SUBMISSION", "请填写本人表达后提交。", 422)
            row.state = "submitted"
            revision = ExpressionRevision(submission_id=row.id, participant_id=row.participant_id,
                                          text=row.text, start=0, end=row.duration, display_name=auth["display_name"],
                                          state="confirmed", confirmed_at=now())
            db.add(revision)
            db.flush()
            repo.audit(db, row.owner_id, "submission_confirmed_by_holder", row.id, participant_id=row.participant_id)
            return _submission_out(db, row, participant=True)

    @app.post("/api/v1/participant/submissions/{submission_id}/withdraw", response_model=SubmissionOut)
    def withdraw(submission_id: str, body: ConfirmIn, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            row = service.owned_participant(db, auth["participant_id"], Submission, submission_id)
            if row.state in ("uploading", "failed"):
                raise DomainError("SUBMISSION_UNAVAILABLE", "投稿尚未完成。", 409)
            row.state = "withdrawn"
            for revision in db.scalars(select(ExpressionRevision).where(ExpressionRevision.submission_id == row.id)):
                revision.state = "withdrawn"
            for adoption in db.scalars(select(Adoption).where(Adoption.submission_id == row.id)):
                adoption.state = "revoked"
            db.flush()
            service.refresh_restrictions(db, row.owner_id, row.episode_id)
            repo.audit(db, row.owner_id, "submission_withdrawn_by_holder", row.id, participant_id=row.participant_id)
            return _submission_out(db, row, participant=True)

    @app.delete("/api/v1/participant/submissions/{submission_id}", response_model=Ok)
    def delete_private_draft(submission_id: str, confirm: bool = False,
                             auth=Depends(participant_auth), repo=Depends(repository)):
        if not confirm:
            raise DomainError("CONFIRM_REQUIRED", "请明确确认删除本机草稿副本；此操作不会删除设备中的外部源文件。", 409)
        with repo.transaction() as db:
            row = service.owned_participant(db, auth["participant_id"], Submission, submission_id)
            submitted = db.scalars(select(ExpressionRevision.id).where(ExpressionRevision.submission_id == row.id)).first()
            adopted = db.scalars(select(Adoption.id).where(Adoption.submission_id == row.id)).first()
            if submitted or adopted:
                raise DomainError("SUBMITTED_DELETE_FORBIDDEN", "该内容已提交过；可撤回使用授权，实体副本按发起人的素材删除流程处理。", 409)
            if row.state not in ("draft", "failed", "withdrawn"):
                raise DomainError("DRAFT_DELETE_UNAVAILABLE", "上传仍在处理中，请等待完成或失败后再删除草稿。", 409)
            row.deleted_at = row.deleted_at or time.time()
            row.state, row.text, row.question_id = "withdrawn", "", None
            for job_id in {row.upload_job_id, row.preview_job_id} - {None}:
                job = db.get(Job, job_id)
                if (job and job.owner_id == row.owner_id and job.episode_id == row.episode_id
                        and job.payload.get("submission_id") == row.id and job.state in ("queued", "running")):
                    job.state, job.stage = "failed", "本人已请求删除私有草稿"
                    job.error = {"code": "SOURCE_REMOVED", "message": "投稿草稿已请求删除。"}
            repo.audit(db, row.owner_id, "private_draft_delete_requested", row.id,
                       participant_id=auth["participant_id"])
        cleanup = service.cleanup_collaboration(repo, submission_id=submission_id,
                                                participant_id=auth["participant_id"])
        if cleanup.pending:
            raise DomainError("DELETION_PENDING", "草稿访问已停止，但本机文件清理尚未完成；请重试删除。", 503)
        return {"ok": True}

    @app.get("/api/v1/episodes/{episode_id}/collaboration/submissions", response_model=SubmissionsOut)
    def owner_submissions(episode_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth["user_id"])
            return {"items": [_submission_out(db, row) for row in db.scalars(select(Submission).where(
                Submission.owner_id == auth["user_id"], Submission.episode_id == episode_id,
                Submission.state.in_(("submitted", "withdrawn")),
                select(ExpressionRevision.id).where(ExpressionRevision.submission_id == Submission.id).exists())
                .order_by(Submission.created_at))]}

    @app.get("/api/v1/collaboration/submissions/{submission_id}/media")
    def owner_media(submission_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.factory() as db:
            row = owner_submission(db, repo, submission_id, auth["user_id"])
            if row.state != "submitted" or row.deleted_at is not None or not row.preview_path:
                raise DomainError("SUBMISSION_UNAVAILABLE", "投稿已撤回或尚未完成预览。", 409)
            return FileResponse(service.controlled_path(repo, row.preview_path, read=True), media_type="video/mp4")

    @app.post("/api/v1/collaboration/submissions/{submission_id}/revisions", status_code=201, response_model=RevisionOut)
    def propose_revision(submission_id: str, body: RevisionIn, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.transaction() as db:
            row = owner_submission(db, repo, submission_id, auth["user_id"])
            if row.state != "submitted" or row.deleted_at is not None:
                raise DomainError("SUBMISSION_UNAVAILABLE", "投稿已撤回。", 409)
            if row.kind != "text" and not 0 <= body.start < body.end <= row.duration:
                raise DomainError("INVALID_SPAN", "表达片段必须落在本人原始投稿内。", 422)
            if row.kind == "text" and (body.start != 0 or body.end != 0):
                raise DomainError("INVALID_SPAN", "文字表达不具有音视频时间区间。", 422)
            revision = ExpressionRevision(submission_id=row.id, participant_id=row.participant_id,
                                          text=body.text, start=body.start, end=body.end,
                                          display_name=db.get(Participant, row.participant_id).display_name)
            db.add(revision)
            db.flush()
            return _revision_out(revision)

    @app.post("/api/v1/participant/revisions/{revision_id}/decision", response_model=RevisionOut)
    def revision_decision(revision_id: str, body: RevisionDecision, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            revision = service.owned_participant(db, auth["participant_id"], ExpressionRevision, revision_id)
            submission = db.get(Submission, revision.submission_id)
            if submission.state != "submitted" or submission.deleted_at is not None:
                raise DomainError("SUBMISSION_UNAVAILABLE", "投稿已撤回或删除。", 409)
            if revision.state != "proposed":
                if revision.state == body.decision:
                    return _revision_out(revision)
                raise DomainError("IMMUTABLE_CONFIRMATION", "该表达已作出决定；请新建修订或撤回投稿。", 409)
            revision.state = body.decision
            revision.confirmed_at = now() if body.decision == "confirmed" else None
            repo.audit(db, submission.owner_id, "expression_decided_by_holder", revision.id,
                       participant_id=revision.participant_id, decision=body.decision)
            return _revision_out(revision)

    @app.post("/api/v1/collaboration/revisions/{revision_id}/adopt", response_model=JobOut)
    def adopt(revision_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.transaction() as db:
            revision = db.get(ExpressionRevision, revision_id)
            if not revision:
                raise DomainError("NOT_FOUND", "表达不存在。", 404)
            row = owner_submission(db, repo, revision.submission_id, auth["user_id"])
            existing = db.scalars(select(Adoption).where(Adoption.revision_id == revision.id)).first()
            if existing:
                service.validate_adoption(db, existing)
                job = db.get(Job, existing.job_id)
                if job.state == "failed":
                    job.state, job.stage, job.error = "queued", "等待重新采用已确认内容", None
                return _job_out(job)
            adoption = Adoption(owner_id=row.owner_id, episode_id=row.episode_id,
                                revision_id=revision.id, submission_id=row.id)
            service.validate_adoption(db, adoption)
            db.add(adoption)
            db.flush()
            job = Job(owner_id=row.owner_id, episode_id=row.episode_id, kind="accept_submission",
                      idempotency_key="adopt:" + revision.id, payload={"adoption_id": adoption.id})
            db.add(job)
            db.flush()
            adoption.job_id = job.id
            return _job_out(job)

    @app.get("/api/v1/participant/objections", response_model=ObjectionsOut)
    def participant_objections(auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.factory() as db:
            return {"items": [_objection_out(db, row) for row in db.scalars(select(Objection).where(
                Objection.participant_id == auth["participant_id"]).order_by(Objection.created_at))]}

    @app.get("/api/v1/episodes/{episode_id}/collaboration/objections", response_model=ObjectionsOut)
    def owner_objections(episode_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.factory() as db:
            repo.owned(db, Episode, episode_id, auth["user_id"])
            return {"items": [_objection_out(db, row) for row in db.scalars(select(Objection).where(
                Objection.owner_id == auth["user_id"], Objection.episode_id == episode_id).order_by(Objection.created_at))]}

    @app.post("/api/v1/participant/objections", status_code=201, response_model=ObjectionOut)
    def object_to_content(body: ObjectionIn, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            invitation = db.get(Invitation, auth["invite_id"])
            asset_id, start, end = None, body.start, body.end
            if not body.reason.strip():
                raise DomainError("INVALID_REQUEST", "请说明本人异议。", 422)
            if body.submission_id:
                submission = service.owned_participant(db, auth["participant_id"], Submission, body.submission_id)
                if submission.state not in ("submitted", "withdrawn"):
                    raise DomainError("SUBMISSION_UNAVAILABLE", "请选择本人已提交内容。", 409)
                if start is not None and (submission.kind == "text" or not 0 <= start < end <= submission.duration):
                    raise DomainError("INVALID_SPAN", "异议区间必须落在本人投稿内。", 422)
            else:
                service.context_source(db, invitation)
                context = invitation.context
                if start is None:
                    start, end = context["start"], context["end"]
                if not context["start"] <= start < end <= context["end"]:
                    raise DomainError("CONTEXT_SCOPE", "只能对本次明确授权的片段提出异议。", 403)
                asset_id = context["asset_id"]
            objection = Objection(owner_id=invitation.owner_id, episode_id=invitation.episode_id,
                                  invite_id=invitation.id, participant_id=auth["participant_id"],
                                  submission_id=body.submission_id, asset_id=asset_id, start=start, end=end,
                                  reason=body.reason.strip())
            db.add(objection)
            db.flush()
            service.refresh_restrictions(db, invitation.owner_id, invitation.episode_id)
            repo.audit(db, invitation.owner_id, "objection_opened_by_holder", objection.id,
                       participant_id=auth["participant_id"])
            return _objection_out(db, objection)

    @app.post("/api/v1/participant/objections/{objection_id}/resolve", response_model=ObjectionOut)
    def resolve_objection(objection_id: str, body: ConfirmIn, auth=Depends(participant_auth), repo=Depends(repository)):
        with repo.transaction() as db:
            objection = service.owned_participant(db, auth["participant_id"], Objection, objection_id)
            objection.state, objection.resolved_at = "resolved", now()
            db.flush()
            service.refresh_restrictions(db, objection.owner_id, objection.episode_id)
            repo.audit(db, objection.owner_id, "objection_resolved_by_holder", objection.id,
                       participant_id=auth["participant_id"])
            return _objection_out(db, objection)
