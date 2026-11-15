"""Local collaboration, immutable expression consent and source-scoped rights.

This module never invokes a model or sends media outside this application.
"""
import hashlib
import os
from pathlib import Path
import re
import time

from sqlalchemy import select

from ..collaboration_models import (Adoption, ExpressionRevision, Invitation, Objection,
                                    Participant, Submission)
from ..models import Asset, Job, Version, now, uid
from ..repository import DomainError
from .artifacts import (artifact_workspace, record_artifact, release_artifact,
                        registered, remove_artifact, protected_output_references)
from .cleanup import CleanupResult, cleanup_deleted_files

COLLAB_JOB_KINDS = ("context_preview", "submission_preview", "accept_submission")
MAX_INTERVIEW_BYTES = 100_000_000
MAX_INTERVIEW_SECONDS = 60


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def accepting_new(invitation):
    return invitation.closed_at is None and invitation.expires_at > time.time()


def require_new(invitation):
    from sqlalchemy.orm import object_session
    from .retention import require_available
    db = object_session(invitation)
    if db is not None:
        require_available(db, invitation.episode_id)
    if not accepting_new(invitation):
        raise DomainError("INVITATION_CLOSED", "邀请已关闭或过期；仍可管理已提交内容与本人权益。", 409)


def owned_participant(db, participant_id, model, item_id):
    from .retention import require_available
    person = db.get(Participant, participant_id)
    invitation = db.get(Invitation, person.invite_id) if person else None
    if invitation:
        require_available(db, invitation.episode_id)
    row = db.get(model, item_id)
    if row is None or row.participant_id != participant_id:
        raise DomainError("NOT_FOUND", "内容不存在或无权访问。", 404)
    return row


def collaboration_path(repo, relative, *, read=False):
    if not isinstance(relative, str) or not re.fullmatch(r"collaboration/[0-9a-f]{32}\.(mp4|mov|wav|mp3|m4a)", relative):
        raise DomainError("INVALID_PATH", "投稿文件位置无效。")
    folder = repo.data_dir / "collaboration"
    if folder.is_symlink() or folder.resolve() != folder:
        raise DomainError("INVALID_PATH", "投稿目录无效。")
    folder.mkdir(exist_ok=True)
    path = folder / Path(relative).name
    if read and (path.is_symlink() or not path.is_file()):
        raise DomainError("SOURCE_MISSING", "投稿副本不可用。", 409)
    return path


def controlled_path(repo, relative, *, read=False):
    if isinstance(relative, str) and relative.startswith("collaboration/"):
        return collaboration_path(repo, relative, read=read)
    if not isinstance(relative, str) or not re.fullmatch(r"(?:assets|outputs)/[0-9a-f]{32}\.(mp4|mov|wav)", relative):
        raise DomainError("INVALID_PATH", "协作产物位置无效。")
    folder = repo.data_dir / Path(relative).parts[0]
    if folder.is_symlink() or folder.resolve() != folder:
        raise DomainError("INVALID_PATH", "协作目录无效。")
    path = folder / Path(relative).name
    if read and (path.is_symlink() or not path.is_file()):
        raise DomainError("SOURCE_MISSING", "协作文件不可用。", 409)
    return path


def shared_quota(db, episode_id):
    """Count original sources once, including retained drafts and withdrawals."""
    adopted_ids = set(db.scalars(select(Adoption.asset_id).where(Adoption.episode_id == episode_id)))
    assets = [a for a in db.scalars(select(Asset).where(Asset.episode_id == episode_id, Asset.deleted == False))
              if a.id not in adopted_ids]
    # A failed physical deletion still occupies its original source quota.
    # cleanup_collaboration clears source_path only after a successful unlink.
    submissions = [s for s in db.scalars(select(Submission).where(Submission.episode_id == episode_id))
                   if s.kind != "text" and s.source_path]
    return {"count": len(assets) + len(submissions),
            "bytes": sum(a.size for a in assets) + sum(s.size for s in submissions),
            "seconds": sum(a.duration for a in assets) + sum(s.duration for s in submissions)}


def check_quota(db, episode_id, settings, *, count=0, size=0, seconds=0):
    usage = shared_quota(db, episode_id)
    if usage["count"] + count > settings.max_assets:
        raise DomainError("TOO_MANY_ASSETS", "本期原始素材与未采用投稿合计最多8段。", 409)
    if usage["bytes"] + size > settings.max_upload_bytes:
        raise DomainError("UPLOAD_TOO_LARGE", "本期原始素材与投稿合计不能超过1 GB。", 413)
    if usage["seconds"] + seconds > settings.max_total_seconds + 0.000001:
        raise DomainError("DURATION_EXCEEDED", "本期原始素材与投稿合计不能超过10分钟。", 409)


def submission_limits(role, settings):
    """Interview caps are additional to the shared original-source quota.

    Ordinary contributed video has the same 600-second / 1-GiB envelope as
    owner uploads; its remaining episode quota is checked atomically at commit.
    These per-file caps also apply to idempotent retries, which do not spend
    remaining quota a second time.
    """
    if role == "interview":
        return MAX_INTERVIEW_BYTES, MAX_INTERVIEW_SECONDS
    if role == "contribute":
        return settings.max_upload_bytes, settings.max_total_seconds
    raise DomainError("ROLE_FORBIDDEN", "此邀请不允许上传投稿。", 403)


def _overlaps(a, b, c, d):
    return a < d - 0.000001 and c < b - 0.000001


def _fixed_expression(invitation, submission, revision):
    return (invitation.role == "interview" or revision.start != 0 or
            abs(revision.end - submission.duration) > 0.000001 or revision.text != submission.text)


def guard_asset(db, asset, segment=None):
    from .retention import require_available
    require_available(db, asset.episode_id)
    if asset.deleted:
        raise DomainError("SOURCE_REMOVED", "引用素材已删除。", 409)
    start, end = (0, asset.duration) if segment is None else (segment["start"], segment["end"])
    adoption = db.scalars(select(Adoption).where(Adoption.asset_id == asset.id)).first()
    revision = submission = None
    if adoption:
        revision = db.get(ExpressionRevision, adoption.revision_id)
        submission = db.get(Submission, adoption.submission_id)
        if (adoption.owner_id != asset.owner_id or adoption.episode_id != asset.episode_id or
                not revision or not submission or submission.owner_id != asset.owner_id or
                submission.episode_id != asset.episode_id or submission.deleted_at is not None or
                adoption.state != "ready" or revision.state != "confirmed" or submission.state != "submitted"):
            raise DomainError("COLLAB_RESTRICTED", "投稿已撤回或本人表达尚未确认，不能继续使用。", 409)
        invitation = db.get(Invitation, submission.invite_id)
        if _fixed_expression(invitation, submission, revision) and segment is not None and not segment.get("_context"):
            if abs(start) > 0.000001 or abs(end - asset.duration) > 0.000001 or segment.get("caption", ""):
                raise DomainError("EXPRESSION_FIXED", "该表达必须使用本人确认的完整片段，不能另截短或加写字幕；请提出新的表达修订。", 409)
    objections = db.scalars(select(Objection).where(Objection.owner_id == asset.owner_id,
                          Objection.episode_id == asset.episode_id, Objection.state == "open"))
    for objection in objections:
        if objection.asset_id == asset.id and _overlaps(start, end, objection.start, objection.end):
            raise DomainError("COLLAB_RESTRICTED", "该片段有尚未由提出者解决的异议。", 409)
        if submission and objection.submission_id == submission.id:
            if objection.start is None or _overlaps(revision.start + start, revision.start + end,
                                                    objection.start, objection.end):
                raise DomainError("COLLAB_RESTRICTED", "该投稿有尚未由提出者解决的异议。", 409)


def asset_details(db, asset):
    adoption = db.scalars(select(Adoption).where(Adoption.asset_id == asset.id)).first()
    if not adoption:
        return {"source_kind": "original", "contributor": None, "interview_text": None, "fixed_segment": None}
    submission = db.get(Submission, adoption.submission_id)
    revision = db.get(ExpressionRevision, adoption.revision_id)
    invitation = db.get(Invitation, submission.invite_id)
    interview = invitation.role == "interview"
    readable = (adoption.state == "ready" and submission.state == "submitted" and
                submission.deleted_at is None and revision.state == "confirmed")
    return {"source_kind": "interview" if interview else "contribution", "contributor": revision.display_name,
            "interview_text": revision.text if interview and readable else None,
            "fixed_segment": {"start": 0, "end": asset.duration}
            if _fixed_expression(invitation, submission, revision) else None}


def refresh_restrictions(db, owner_id, episode_id):
    """A resolved objection restores preview only; confirmation is never restored."""
    from .sharing import suspend_version_shares
    for version in db.scalars(select(Version).where(Version.owner_id == owner_id, Version.episode_id == episode_id)):
        restricted = None
        for segment in version.segments:
            asset = db.get(Asset, segment["asset_id"])
            if asset and asset.owner_id == owner_id:
                try:
                    guard_asset(db, asset, segment)
                except DomainError as exc:
                    if exc.code in ("COLLAB_RESTRICTED", "SOURCE_REMOVED"):
                        if restricted is None or exc.code == "SOURCE_REMOVED":
                            restricted = exc
        if restricted:
            version.state, version.confirmed_at = "restricted", None
            version.error = {"code": restricted.code, "message": restricted.message}
            suspend_version_shares(db, [version.id], reason=restricted.code)
        elif version.state == "restricted" and (version.error or {}).get("code") == "COLLAB_RESTRICTED":
            # Do not unlock versions whose other sources have since been deleted.
            valid = True
            for segment in version.segments:
                asset = db.get(Asset, segment["asset_id"])
                try:
                    if not asset or asset.owner_id != owner_id:
                        valid = False
                        break
                    guard_asset(db, asset, segment)
                except DomainError:
                    valid = False
            if valid:
                version.state = "ready" if version.output_path else "draft"
                version.confirmed_at, version.error = None, None


def context_source(db, invitation):
    context = invitation.context
    if not context:
        raise DomainError("NO_CONTEXT", "此邀请没有可访问的上下文。", 404)
    asset = db.get(Asset, context["asset_id"])
    if not asset or asset.owner_id != invitation.owner_id or asset.episode_id != invitation.episode_id:
        raise DomainError("NOT_FOUND", "上下文不可用。", 404)
    if not 0 <= context["start"] < context["end"] <= asset.duration or context["end"] - context["start"] > 30:
        raise DomainError("INVALID_CONTEXT", "上下文区间已失效。", 409)
    # Context is an authorization to review this range, not permission to edit
    # the interview. A pending objection does not erase its reporter's evidence.
    if asset.deleted:
        raise DomainError("SOURCE_REMOVED", "上下文素材已删除。", 409)
    adoption = db.scalars(select(Adoption).where(Adoption.asset_id == asset.id)).first()
    if adoption:
        submission = db.get(Submission, adoption.submission_id)
        if adoption.state != "ready" or not submission or submission.state != "submitted" or submission.deleted_at:
            raise DomainError("COLLAB_RESTRICTED", "上下文授权已撤回。", 409)
    return asset


def validate_adoption(db, adoption):
    submission = db.get(Submission, adoption.submission_id)
    revision = db.get(ExpressionRevision, adoption.revision_id)
    if (not submission or not revision or revision.submission_id != submission.id or
            revision.participant_id != submission.participant_id or submission.owner_id != adoption.owner_id or
            submission.episode_id != adoption.episode_id or submission.state != "submitted" or
            submission.deleted_at is not None or revision.state != "confirmed" or adoption.state == "revoked"):
        raise DomainError("CONSENT_REQUIRED", "本人未确认该表达，或投稿已撤回。", 409)
    if submission.kind == "text":
        raise DomainError("MEDIA_REQUIRED", "文字可核对保留；本轮需本人语音或视频才能生成采访片段。", 409)
    invitation = db.get(Invitation, submission.invite_id)
    if invitation.role == "contribute" and _fixed_expression(invitation, submission, revision):
        from .media import MAX_OUTPUT_SECONDS
        if revision.end - revision.start > MAX_OUTPUT_SECONDS + 0.000001:
            raise DomainError("EXPRESSION_TOO_LONG",
                              "已确认的改写片段须完整用于成片，不能超过90秒。请提出更短的新修订并由本人确认；原始供材仍可全长采用后选段。", 409)
    for objection in db.scalars(select(Objection).where(Objection.submission_id == submission.id,
                                                       Objection.state == "open")):
        if objection.start is None or _overlaps(revision.start, revision.end, objection.start, objection.end):
            raise DomainError("COLLAB_RESTRICTED", "该投稿存在未解决的本人异议。", 409)
    return submission, revision, invitation


def _job_context(db, job):
    if not job or job.state != "running" or job.kind not in COLLAB_JOB_KINDS:
        raise DomainError("JOB_INACTIVE", "协作任务已失效。", 409)
    from .retention import require_available
    require_available(db, job.episode_id)
    if job.kind == "context_preview":
        row = db.get(Invitation, job.payload.get("invite_id"))
        if not row or row.owner_id != job.owner_id or row.episode_id != job.episode_id:
            raise DomainError("NOT_FOUND", "邀请不存在。", 404)
        source = context_source(db, row)
        return row, source
    if job.kind == "submission_preview":
        row = db.get(Submission, job.payload.get("submission_id"))
        if (not row or row.owner_id != job.owner_id or row.episode_id != job.episode_id or
                row.state != "draft" or row.deleted_at is not None):
            raise DomainError("SUBMISSION_UNAVAILABLE", "投稿已不可预览。", 409)
        return row, db.get(Participant, row.participant_id)
    adoption = db.get(Adoption, job.payload.get("adoption_id"))
    if not adoption or adoption.owner_id != job.owner_id or adoption.episode_id != job.episode_id:
        raise DomainError("NOT_FOUND", "采用记录不存在。", 404)
    return adoption, validate_adoption(db, adoption)


def run_collaboration_job(repo, job_id):
    """Called by the serial worker after it durably marks the Job running."""
    from . import interview_media, media
    output = relative = None
    published = False
    created_output = False
    with repo.factory() as db:
        job = db.get(Job, job_id)
        kind = job.kind
        row, source = _job_context(db, job)
        if kind == "context_preview":
            input_path = repo.path(source.path)
            context = row.context
        elif kind == "submission_preview":
            input_path = collaboration_path(repo, row.source_path, read=True)
            media_kind, label = row.kind, source.display_name
            contribution_preview = db.get(Invitation, row.invite_id).role == "contribute"
        else:
            submission, revision, invitation = source
            input_path = collaboration_path(repo, submission.source_path, read=True)
            media_kind, label = submission.kind, revision.display_name
            start, end, expression = revision.start, revision.end, revision.text
            is_interview = invitation.role == "interview"
            fixed_contribution = not is_interview and _fixed_expression(invitation, submission, revision)
    try:
        if kind == "accept_submission":
            suffix = ".mp4"
            with repo.transaction() as db:
                job = db.get(Job, job_id)
                adoption, _ = _job_context(db, job)
                if adoption.output_path:
                    try:
                        controlled_path(repo, adoption.output_path).unlink(missing_ok=True)
                    except OSError:
                        raise DomainError("CLEANUP_PENDING", "上次采用的临时文件尚未清理，请稍后重试。", 503) from None
                # An exact persisted target owns an orphan if the process dies
                # between filesystem publication and the database commit.
                relative = "assets/" + uid() + suffix
                candidate = controlled_path(repo, relative)
                if candidate.exists() or candidate.is_symlink():
                    raise DomainError("ARTIFACT_EXISTS", "采用文件位置已存在，请重试以生成新位置。", 409)
                adoption.output_path = relative
            output = controlled_path(repo, relative)
        else:
            relative = "outputs/" + uid() + ".mp4"
            record_artifact(repo, job_id, relative)
            output = controlled_path(repo, relative)
        with artifact_workspace(repo, job_id) as workspace:
            complete = workspace / ("complete" + output.suffix)
            if kind == "context_preview":
                info = interview_media.context_clip(input_path, complete, context["start"], context["end"])
            elif kind == "submission_preview":
                if contribution_preview:
                    info = interview_media.preview_contribution(input_path, complete)
                else:
                    info = interview_media.materialize_interview(input_path, media_kind, complete, label)
            elif is_interview:
                info = interview_media.materialize_interview(input_path, media_kind, complete, label,
                                                            start=start, end=end, expression=expression)
            elif fixed_contribution:
                # The agreed revision is its own immutable materialised clip.
                # The literal label distinguishes the annotation from an ASR
                # transcript or words supposedly spoken in the original scene.
                caption = "本人确认补充：" + expression if expression else ""
                info = media.render([{"path": input_path, "start": start, "end": end, "caption": caption}],
                                    complete, title="")
            else:
                # Ordinary supplied video also gets a browser-compatible copy,
                # with no interview identity or added expression. Its untouched
                # original remains in the Submission's restricted source store.
                original_info = media.probe(input_path)
                info = interview_media.preview_contribution(input_path, complete)
                info["has_audio"] = original_info["has_audio"]
            with repo.transaction() as db:
                job = db.get(Job, job_id)
                current, _ = _job_context(db, job)
                os.link(complete, output)
                created_output = True
                if kind == "context_preview":
                    current.context_path = relative
                elif kind == "submission_preview":
                    current.preview_path = relative
                else:
                    submission, revision, invitation = validate_adoption(db, current)
                    asset = Asset(owner_id=job.owner_id, episode_id=job.episode_id,
                                  filename=("事后采访-" if invitation.role == "interview" else "朋友供材-") + revision.display_name + output.suffix,
                                  path=relative, size=output.stat().st_size,
                                  **{key: info[key] for key in ("duration", "width", "height", "has_audio")})
                    db.add(asset)
                    db.flush()
                    current.asset_id, current.state = asset.id, "ready"
                    # New adopted material is available to the next explicit
                    # analysis; it never changes old directions or versions here.
                release_artifact(db, job, relative)
                job.state, job.stage, job.error = "succeeded", "本机处理完成", None
                job.result = {"asset_id": current.asset_id} if kind == "accept_submission" else {"ready": True}
                repo.audit(db, job.owner_id, kind + "_succeeded", job.id)
            published = True
    finally:
        if output is not None and not published and created_output:
            try:
                output.unlink(missing_ok=True)
            except OSError:
                pass
        elif output is not None and kind == "accept_submission" and not published:
            # A known failed exclusive publication never owns an entry that was
            # already there. Clear its planned target so recovery cannot unlink it.
            with repo.transaction() as db:
                adoption = db.get(Adoption, row.id)
                if adoption and adoption.output_path == relative and adoption.state == "pending":
                    adoption.output_path = None


def cleanup_collaboration(repo, *, submission_id=None, participant_id=None, owner_id=None, episode_id=None):
    """Only explicit deletion or a failed upload/acceptance removes local bytes.

    Withdrawal, expiry and invitation closure revoke rights but retain local
    files. Interrupted temporary trees are handled by the Job artifact manifest.
    """
    targets = set()
    artifact_targets = {}
    artifact_owners = {}
    invalid = 0
    deleted_owners = set()
    with repo.transaction() as db:
        submissions_query = select(Submission)
        if submission_id is not None:
            submissions_query = submissions_query.where(Submission.id == submission_id)
        if participant_id is not None:
            submissions_query = submissions_query.where(Submission.participant_id == participant_id)
        if owner_id is not None:
            submissions_query = submissions_query.where(Submission.owner_id == owner_id)
        if episode_id is not None:
            submissions_query = submissions_query.where(Submission.episode_id == episode_id)
        submissions = list(db.scalars(submissions_query))
        submission_ids = {row.id for row in submissions}
        for adoption in db.scalars(select(Adoption).where(Adoption.submission_id.in_(submission_ids))):
            asset = db.get(Asset, adoption.asset_id) if adoption.asset_id else None
            job = db.get(Job, adoption.job_id) if adoption.job_id else None
            if asset and asset.deleted:
                submission = db.get(Submission, adoption.submission_id)
                submission.deleted_at = submission.deleted_at or time.time()
                submission.state, adoption.state = "withdrawn", "revoked"
            elif adoption.state == "pending" and job and job.state == "failed" and adoption.output_path:
                targets.add(adoption.output_path)
        for submission in submissions:
            upload_job = db.get(Job, submission.upload_job_id) if submission.upload_job_id else None
            if submission.state == "uploading" and upload_job and upload_job.state == "failed":
                submission.state = "failed"
                submission.deleted_at = submission.deleted_at or time.time()
            if submission.deleted_at is not None:
                if submission.source_path:
                    targets.add(submission.source_path)
                if submission.preview_path:
                    targets.add(submission.preview_path)
                job_ids = {submission.upload_job_id, submission.preview_job_id} - {None}
                adoption_ids = set()
                for adoption in db.scalars(select(Adoption).where(Adoption.submission_id == submission.id)):
                    adoption_ids.add(adoption.id)
                    if adoption.job_id:
                        job_ids.add(adoption.job_id)
                    adoption.state = "revoked"
                    if adoption.output_path:
                        targets.add(adoption.output_path)
                    asset = db.get(Asset, adoption.asset_id) if adoption.asset_id else None
                    if asset:
                        asset.deleted = True
                        deleted_owners.add(submission.owner_id)
                for job in db.scalars(select(Job).where(Job.id.in_(job_ids))):
                    if (job.owner_id != submission.owner_id or job.episode_id != submission.episode_id or
                            not (job.payload.get("submission_id") == submission.id or
                                 job.payload.get("adoption_id") in adoption_ids)):
                        continue
                    if job.state in ("queued", "running"):
                        job.state, job.stage = "failed", "来源已删除，协作处理已停止"
                        job.error = {"code": "SOURCE_REMOVED", "message": "投稿副本已请求删除。"}
                    for item in registered(job):
                        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                            invalid += 1
                            continue
                        relative, kind = item["path"], item.get("kind")
                        artifact_targets[relative] = kind
                        artifact_owners.setdefault(relative, set()).add(job.id)
                for revision in db.scalars(select(ExpressionRevision).where(ExpressionRevision.submission_id == submission.id)):
                    revision.state = "withdrawn"
                refresh_restrictions(db, submission.owner_id, submission.episode_id)
        if submission_id is None and participant_id is None:
            invitations_query = select(Invitation)
            if owner_id is not None:
                invitations_query = invitations_query.where(Invitation.owner_id == owner_id)
            if episode_id is not None:
                invitations_query = invitations_query.where(Invitation.episode_id == episode_id)
            for invitation in db.scalars(invitations_query):
                asset_id = (invitation.context or {}).get("asset_id")
                asset = db.get(Asset, asset_id) if asset_id else None
                if invitation.context and (not asset or asset.deleted):
                    invitation.closed_at = invitation.closed_at or time.time()
                    if invitation.context_path:
                        targets.add(invitation.context_path)
                    job = db.get(Job, invitation.context_job_id) if invitation.context_job_id else None
                    if (job and job.owner_id == invitation.owner_id and job.episode_id == invitation.episode_id
                            and job.payload.get("invite_id") == invitation.id):
                        if job.state in ("queued", "running"):
                            job.state, job.stage = "failed", "来源已删除，上下文处理已停止"
                            job.error = {"code": "SOURCE_REMOVED", "message": "素材副本已请求删除。"}
                        for item in registered(job):
                            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                                invalid += 1
                                continue
                            relative, kind = item["path"], item.get("kind")
                            artifact_targets[relative] = kind
                            artifact_owners.setdefault(relative, set()).add(job.id)
        protected = protected_output_references(repo, db)
    pending = invalid
    removed = set()
    all_targets = targets.union(artifact_targets)
    for relative in all_targets:
        try:
            if relative in targets:
                controlled_path(repo, relative).unlink(missing_ok=True)
            else:
                if relative in protected:
                    pending += 1
                    continue
                remove_artifact(repo, relative, artifact_targets[relative])
            removed.add(relative)
        except (OSError, DomainError, TypeError):
            pending += 1
    if removed:
        with repo.transaction() as db:
            for submission in db.scalars(select(Submission).where(Submission.id.in_(submission_ids), Submission.deleted_at.is_not(None))):
                if submission.source_path in removed:
                    submission.source_path = None
                if submission.preview_path in removed:
                    submission.preview_path = None
            for relative in removed.intersection(artifact_owners):
                for job_id in artifact_owners[relative]:
                    job = db.get(Job, job_id)
                    if job:
                        release_artifact(db, job, relative)
    attempted = len(all_targets) + invalid
    # A single original may have several adopted revisions. Once all their
    # assets/versions are durably revoked, clean every marked dependent output.
    for owner_id in deleted_owners:
        result = cleanup_deleted_files(repo, owner_id=owner_id)
        attempted += result.attempted
        pending += result.pending
    return CleanupResult(attempted=attempted, pending=pending)
