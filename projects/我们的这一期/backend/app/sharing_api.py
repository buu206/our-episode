"""Owner share controls and separately authenticated, non-export viewer routes."""
import hmac
import secrets
import time

from fastapi import Depends, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from sqlalchemy import select

from .models import Episode, Version, now
from .repository import DomainError
from .schemas import Ok
from .sharing_models import Share, ViewerSession
from .sharing_schemas import (ShareClose, ShareCreate, ShareCreatedOut, ShareOut, SharesOut,
                              ShareUpdate, ViewerAuthOut, ViewerLogin, ViewerShareOut)
from .services import sharing as service


def register_sharing(app, owner_session, repository, settings, cookie_name):
    viewer_cookie = cookie_name + "_viewer"

    def viewer_session(request: Request, repo=Depends(repository)):
        token = request.cookies.get(viewer_cookie, "")
        with repo.factory() as db:
            row = db.get(ViewerSession, service.digest(token)) if token else None
            if not row or row.expires_at <= time.time():
                raise DomainError("VIEWER_AUTH_REQUIRED", "请使用这条分享的观看码进入。", 401)
            if request.method not in ("GET", "HEAD") and not hmac.compare_digest(
                    row.csrf, request.headers.get("x-csrf-token", "")):
                raise DomainError("CSRF_FAILED", "观看会话校验失败，请刷新后重试。", 403)
            return {"share_id": row.share_id, "csrf_token": row.csrf, "token_hash": row.token_hash}

    def viewer_share(db, share_id, auth):
        share = db.get(Share, share_id)
        if auth["share_id"] != share_id or share is None:
            raise DomainError("NOT_FOUND", "该分享不存在或不属于当前观看授权。", 404)
        return share

    @app.post("/api/v1/versions/{version_id}/shares", response_model=ShareCreatedOut, status_code=201)
    def create_share(version_id: str, body: ShareCreate, auth=Depends(owner_session), repo=Depends(repository)):
        fingerprint = service.request_fingerprint("create", version_id, body.model_dump(mode="json"))
        code = None
        with repo.transaction() as db:
            operation = service.find_operation(db, auth["user_id"], body.client_request_id, "create", fingerprint)
            if operation:
                result = operation.result
            else:
                version = repo.owned(db, Version, version_id, auth["user_id"])
                snapshot, media = service.candidate_snapshot(repo, db, version)
                media.close()
                code = secrets.token_urlsafe(32)
                share = Share(owner_id=auth["user_id"], episode_id=version.episode_id, label=body.label,
                              code_hash=service.digest(code), expires_at=time.time() + body.expires_days * 86400,
                              **snapshot)
                db.add(share)
                db.flush()
                result = service.share_out(share)
                service.record_operation(db, auth["user_id"], body.client_request_id, "create", fingerprint, share, result)
                repo.audit(db, auth["user_id"], "share_created", share.id, version_id=version.id, revision=share.revision)
        return {**result, "share_code": code,
                "watch_url": result["watch_path"] + "#code=" + code if code else None}

    @app.get("/api/v1/episodes/{episode_id}/shares", response_model=SharesOut)
    def list_shares(episode_id: str, auth=Depends(owner_session), repo=Depends(repository)):
        with repo.transaction() as db:
            repo.owned(db, Episode, episode_id, auth["user_id"])
            shares = list(db.scalars(select(Share).where(Share.owner_id == auth["user_id"], Share.episode_id == episode_id)
                                     .order_by(Share.created_at)))
            for share in shares:
                service.inspect_share(db, repo, share)
            return {"items": [service.share_out(share) for share in shares]}

    @app.post("/api/v1/shares/{share_id}/update", response_model=ShareOut)
    def update_share(share_id: str, body: ShareUpdate, auth=Depends(owner_session), repo=Depends(repository)):
        fingerprint = service.request_fingerprint("update", share_id, body.model_dump(mode="json"))
        fault = None
        result = None
        with repo.transaction() as db:
            share = repo.owned(db, Share, share_id, auth["user_id"])
            service.inspect_share(db, repo, share)
            operation = None
            if share.state in ("closed", "expired"):
                fault = service.state_error(share)
            else:
                try:
                    operation = service.find_operation(db, auth["user_id"], body.client_request_id, "update", fingerprint)
                except DomainError as exc:
                    fault = exc
            if fault:
                pass
            elif operation:
                # Replaying an old successful action must not suggest that a
                # later restriction or publication has been undone.
                result = service.share_out(share)
            elif body.expected_revision != share.revision:
                fault = DomainError("SHARE_REVISION_CONFLICT", "分享已被另一操作更新，请刷新后重新确认。", 409)
            else:
                try:
                    version = repo.owned(db, Version, body.version_id, auth["user_id"])
                    if version.episode_id != share.episode_id:
                        raise DomainError("SHARE_EPISODE_MISMATCH", "分享只能明确更新到同一期中的已确认版本。", 409)
                    snapshot, media = service.candidate_snapshot(repo, db, version)
                    media.close()
                except DomainError as exc:
                    fault = exc
                else:
                    for key, value in snapshot.items():
                        setattr(share, key, value)
                    share.revision += 1
                    share.state, share.restricted_reason, share.updated_at = "active", None, now()
                    result = service.share_out(share)
                    service.record_operation(db, auth["user_id"], body.client_request_id, "update", fingerprint, share, result)
                    repo.audit(db, auth["user_id"], "share_explicitly_updated", share.id,
                               version_id=share.version_id, revision=share.revision)
        if fault:
            raise fault
        return result

    @app.post("/api/v1/shares/{share_id}/close", response_model=ShareOut)
    def close_share(share_id: str, body: ShareClose, auth=Depends(owner_session), repo=Depends(repository)):
        fault = None
        with repo.transaction() as db:
            share = repo.owned(db, Share, share_id, auth["user_id"])
            service.inspect_share(db, repo, share)
            if body.expected_revision != share.revision:
                fault = DomainError("SHARE_REVISION_CONFLICT", "分享已更新，请刷新后确认要关闭的发布修订。", 409)
            elif share.state == "expired":
                fault = service.state_error(share)
            elif share.state != "closed":
                share.state, share.closed_at, share.updated_at = "closed", time.time(), now()
                repo.audit(db, auth["user_id"], "share_closed", share.id, revision=share.revision)
            result = service.share_out(share)
        if fault:
            raise fault
        return result

    @app.post("/api/v1/viewer/auth/login", response_model=ViewerAuthOut)
    def viewer_login(body: ViewerLogin, response: Response, repo=Depends(repository)):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        fault = None
        with repo.transaction() as db:
            share = db.get(Share, body.share_id)
            if not share or not hmac.compare_digest(share.code_hash, service.digest(body.share_code)):
                raise DomainError("INVALID_SHARE_CODE", "观看码无效。", 401)
            _, fault = service.inspect_share(db, repo, share)
            if not fault:
                db.add(ViewerSession(token_hash=service.digest(token), share_id=share.id, csrf=csrf,
                                     expires_at=time.time() + settings.session_seconds))
                result = {"share_id": share.id, "label": share.label, "csrf_token": csrf}
        if fault:
            raise fault
        response.set_cookie(viewer_cookie, token, max_age=settings.session_seconds,
                            httponly=True, samesite="strict", path="/")
        return result

    @app.get("/api/v1/viewer/auth/me", response_model=ViewerAuthOut)
    def viewer_me(auth=Depends(viewer_session), repo=Depends(repository)):
        with repo.transaction() as db:
            share = viewer_share(db, auth["share_id"], auth)
            _, fault = service.inspect_share(db, repo, share)
            result = {"share_id": share.id, "label": share.label, "csrf_token": auth["csrf_token"]}
        if fault:
            raise fault
        return result

    @app.post("/api/v1/viewer/auth/logout", response_model=Ok)
    def viewer_logout(response: Response, auth=Depends(viewer_session), repo=Depends(repository)):
        with repo.transaction() as db:
            row = db.get(ViewerSession, auth["token_hash"])
            if row:
                db.delete(row)
        response.delete_cookie(viewer_cookie, path="/")
        return {"ok": True}

    @app.get("/api/v1/viewer/shares/{share_id}", response_model=ViewerShareOut)
    def watch_share(share_id: str, auth=Depends(viewer_session), repo=Depends(repository)):
        with repo.transaction() as db:
            share = viewer_share(db, share_id, auth)
            _, fault = service.inspect_share(db, repo, share)
            result = {key: getattr(share, key) for key in
                      ("id", "label", "title", "version_id", "revision", "expires_at", "confirmed_at")}
            result["media_url"] = f"/api/v1/viewer/shares/{share.id}/media?revision={share.revision}"
        if fault:
            raise fault
        return result

    @app.api_route("/api/v1/viewer/shares/{share_id}/media", methods=["GET", "HEAD"])
    def watch_media(share_id: str, request: Request, revision: int = Query(ge=1),
                    auth=Depends(viewer_session), repo=Depends(repository)):
        with repo.transaction() as db:
            share = viewer_share(db, share_id, auth)
            handle, fault = service.inspect_share(db, repo, share, expected_revision=revision, keep_media=True)
        if fault:
            raise fault
        headers = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache", "Expires": "0",
                   "Accept-Ranges": "bytes", "Content-Type": "video/mp4", "Content-Disposition": "inline"}
        try:
            start, end, status = service.byte_range(request.headers.get("range"), handle.size)
        except DomainError as exc:
            handle.close()
            headers["Content-Type"] = "application/json"
            headers["Content-Range"] = f"bytes */{handle.size}"
            return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, exc.status, headers=headers)
        headers["Content-Length"] = str(end - start + 1)
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{handle.size}"
        if request.method == "HEAD":
            handle.close()
            return Response(status_code=status, headers=headers)
        return StreamingResponse(service.media_chunks(repo, share_id, revision, handle, start, end),
                                 status_code=status, headers=headers, background=BackgroundTask(handle.close))
