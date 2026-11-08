"""Read-only deletion status from revoked assets and exact persisted references.

No directory scans, unlink operations or participant content are exposed here.
A missing registered file is already clean; an unsafe reference or stat failure
remains pending so the UI cannot claim deletion that could not be established.
"""
from pathlib import Path
import re

from sqlalchemy import select

from ..collaboration_models import Adoption, Invitation, Submission
from ..models import Asset, Job, Version
from ..repository import DomainError
from .artifacts import artifact_path, output_reference
from .cleanup import _application_path


def _exists(repo, relative, category, kind="file"):
    if relative is None:
        return False
    try:
        if not isinstance(relative, str):
            return True
        if category == "artifact":
            path = artifact_path(repo, relative, kind)
        elif category == "collaboration":
            if not re.fullmatch(r"collaboration/[0-9a-f]{32}\.(?:mp4|mov|wav|mp3|m4a)", relative):
                return True
            path = _application_path(repo, relative, "collaboration")
        else:
            path = _application_path(repo, relative, category)
        path.lstat()  # A dangling symlink is a file still requiring unlink.
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError, DomainError):
        return True
    return True


def asset_cleanup_pending(db, repo, asset):
    """Include the original's adopted siblings, without inspecting other owners."""
    if not asset.deleted:
        return False
    owner, episode = asset.owner_id, asset.episode_id
    adoptions = list(db.scalars(select(Adoption).where(
        Adoption.owner_id == owner, Adoption.episode_id == episode)))
    submission_ids = {row.submission_id for row in adoptions if row.asset_id == asset.id}
    family_adoptions = [row for row in adoptions if row.submission_id in submission_ids]
    family = {asset.id, *(row.asset_id for row in family_adoptions if row.asset_id)}
    assets = list(db.scalars(select(Asset).where(
        Asset.id.in_(family), Asset.owner_id == owner, Asset.episode_id == episode)))
    family = {row.id for row in assets}
    if any(_exists(repo, row.path, "assets") for row in assets):
        return True

    versions = [row for row in db.scalars(select(Version).where(
        Version.owner_id == owner, Version.episode_id == episode))
        if any(item.get("asset_id") in family for item in row.segments)]
    if any(_exists(repo, relative, "outputs") for row in versions
           for relative in (row.output_path, row.narration_path)):
        return True

    related_jobs = set()
    adoption_ids = {row.id for row in family_adoptions}
    for row in family_adoptions:
        if _exists(repo, row.output_path, "assets"):
            return True
        if row.job_id:
            related_jobs.add(row.job_id)
    for row in db.scalars(select(Submission).where(Submission.id.in_(submission_ids),
            Submission.owner_id == owner, Submission.episode_id == episode)):
        if _exists(repo, row.source_path, "collaboration") or _exists(repo, row.preview_path, "outputs"):
            return True
        related_jobs.update(value for value in (row.upload_job_id, row.preview_job_id) if value)

    invitation_ids = set()
    for row in db.scalars(select(Invitation).where(Invitation.owner_id == owner, Invitation.episode_id == episode)):
        if (row.context or {}).get("asset_id") not in family:
            continue
        invitation_ids.add(row.id)
        if _exists(repo, row.context_path, "outputs"):
            return True
        if row.context_job_id:
            related_jobs.add(row.context_job_id)

    version_ids = {row.id for row in versions}
    for job in db.scalars(select(Job).where(Job.owner_id == owner, Job.episode_id == episode)):
        payload = job.payload
        if not isinstance(payload, dict):
            if job.version_id in version_ids or job.id in related_jobs:
                return True
            continue
        linked = bool(family.intersection(payload.get("asset_ids", []))) or job.version_id in version_ids
        linked = linked or (job.id in related_jobs and (
            payload.get("submission_id") in submission_ids or payload.get("adoption_id") in adoption_ids or
            payload.get("invite_id") in invitation_ids))
        if not linked:
            continue
        steps = payload.get("steps", {})
        tts = steps.get("tts", {}) if isinstance(steps, dict) else {}
        voice = tts.get("path") if isinstance(tts, dict) else None
        if voice is not None:
            relative = output_reference(repo, voice)
            if not relative or _exists(repo, relative, "artifact"):
                return True
        artifacts = payload.get("artifacts", [])
        if not isinstance(artifacts, list):
            return True
        for item in artifacts:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return True
            if _exists(repo, item["path"], "artifact", item.get("kind")):
                return True
    return False


def pending_cleanup(db, repo, episode):
    return [{"asset_id": asset.id, "filename": asset.filename}
        for asset in db.scalars(select(Asset).where(Asset.owner_id == episode.owner_id,
            Asset.episode_id == episode.id, Asset.deleted == True).order_by(Asset.created_at))
        if asset_cleanup_pending(db, repo, asset)]
