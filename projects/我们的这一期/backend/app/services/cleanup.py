"""Retry physical cleanup only for durable, explicitly revoked application files.

Asset.deleted and Version.state='restricted' remain persisted even when unlink
fails. They are both access-control state and the recoverable cleanup manifest;
no directory sweep or deletion of unmarked files is performed here.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from ..models import Asset, Job, Version
from ..repository import DomainError
from .artifacts import artifact_path, output_reference, registered, release_artifact, remove_artifact


@dataclass(frozen=True)
class CleanupResult:
    attempted: int
    pending: int


def _application_path(repo, relative: str, directory: str) -> Path:
    # Paths are DB-owned, but refuse malformed records rather than letting a
    # cleanup operation reach another area such as the database or source files.
    value = Path(relative)
    if value.is_absolute() or len(value.parts) != 2 or value.parts[0] != directory:
        raise ValueError("invalid cleanup reference")
    folder = repo.data_dir / directory
    if folder.is_symlink() or folder.resolve().parent != repo.data_dir:
        raise ValueError("invalid application directory")
    # Do not resolve the final entry: if replaced with a symlink, unlink removes
    # the link itself, never its target. Parent directories are fixed above.
    return folder / value.name


def cleanup_deleted_files(repo, *, asset_id: str | None = None,
                          owner_id: str | None = None) -> CleanupResult:
    targets: set[tuple[str, str, str]] = set()
    releases: dict[str, set[str]] = {}
    protected_other_tasks: set[str] = set()
    invalid = 0
    with repo.factory() as db:
        assets_query = select(Asset).where(Asset.deleted == True)
        versions_query = select(Version).where(Version.state == "restricted")
        if owner_id is not None:
            assets_query = assets_query.where(Asset.owner_id == owner_id)
            versions_query = versions_query.where(Version.owner_id == owner_id)
        if asset_id is not None:
            assets_query = assets_query.where(Asset.id == asset_id)
        assets = list(db.scalars(assets_query))
        deleted_by_scope = {}
        affected_versions = set()
        for asset in assets:
            deleted_by_scope.setdefault((asset.owner_id, asset.episode_id), set()).add(asset.id)
        for asset in assets:
            targets.add(("assets", asset.path, "file"))
        for version in db.scalars(versions_query):
            scoped = deleted_by_scope.get((version.owner_id, version.episode_id), set())
            if any(item.get('asset_id') in scoped for item in version.segments):
                affected_versions.add((version.owner_id, version.episode_id, version.id))
                if version.output_path:
                    targets.add(("outputs", version.output_path, "file"))
                if version.narration_path:
                    targets.add(("outputs", version.narration_path, "file"))
        # Paid audio can have been checkpointed before the Version pointer was
        # committed. Its exact persisted path is part of the same deletion scope.
        for job in db.scalars(select(Job)):
            scoped = deleted_by_scope.get((job.owner_id, job.episode_id), set())
            affected = bool(scoped.intersection(job.payload.get('asset_ids', []))) or (
                job.owner_id, job.episode_id, job.version_id) in affected_versions
            if affected:
                recorded = job.payload.get('steps', {}).get('tts', {}).get('path')
                if recorded:
                    relative = output_reference(repo, recorded)
                    if relative:
                        targets.add(('outputs', relative, 'file'))
                    else:
                        invalid += 1
                for item in registered(job):
                    try:
                        relative, kind = item['path'], item['kind']
                        artifact_path(repo, relative, kind)
                        targets.add((Path(relative).parts[0], relative, kind))
                        releases.setdefault(relative, set()).add(job.id)
                    except (DomainError, KeyError, TypeError):
                        invalid += 1
            elif job.state == 'running':
                # A malformed duplicated registration must not let a deletion
                # request remove an unrelated task's active workspace/output.
                for item in registered(job):
                    if isinstance(item, dict) and isinstance(item.get('path'), str):
                        protected_other_tasks.add(item['path'])

    pending = invalid
    completed = set()
    for directory, relative, kind in sorted(targets):
        if relative in protected_other_tasks:
            pending += 1
            continue
        try:
            if kind == 'directory':
                remove_artifact(repo, relative, kind)
            else:
                _application_path(repo, relative, directory).unlink(missing_ok=True)
            completed.add(relative)
        except (OSError, TypeError, ValueError, DomainError):
            # Keep the database markers intact for another explicit retry or
            # startup. Do not include paths or exception text in client errors.
            pending += 1
    if completed.intersection(releases):
        with repo.transaction() as db:
            for relative in completed.intersection(releases):
                for job_id in releases[relative]:
                    job = db.get(Job, job_id)
                    if job is not None:
                        release_artifact(db, job, relative)
    return CleanupResult(attempted=len(targets) + invalid, pending=pending)
