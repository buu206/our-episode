"""Persist temporary artifact ownership before writing; never sweep directories."""

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import shutil

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..models import Job, Version, uid
from ..repository import DomainError


@dataclass(frozen=True)
class ArtifactRecovery:
    attempted: int = 0
    pending: int = 0
    retained: int = 0


def artifact_path(repo, relative: str, kind: str = "file") -> Path:
    """Validate lexically and do not resolve the final entry through a symlink."""
    pattern = r"outputs/[0-9a-f]{32}\.(?:wav|mp4)" if kind == "file" else r"incoming/work-[0-9a-f]{32}"
    if kind not in ("file", "directory") or not isinstance(relative, str) or not re.fullmatch(pattern, relative):
        raise DomainError("INVALID_ARTIFACT", "临时产物路径不在受控范围内。")
    value = Path(relative)
    folder = repo.data_dir / value.parts[0]
    if folder.is_symlink() or folder.resolve() != repo.data_dir / value.parts[0]:
        raise DomainError("INVALID_ARTIFACT", "临时产物目录无效。")
    return folder / value.name


def output_reference(repo, value) -> str | None:
    """Normalise a persisted output reference, including speech's absolute Path."""
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(repo.data_dir)
        except ValueError:
            return None
    relative = path.as_posix()
    try:
        artifact_path(repo, relative)
    except DomainError:
        return None
    return relative


def registered(job) -> list[dict]:
    values = job.payload.get("artifacts", [])
    return values if isinstance(values, list) else []


def remove_artifact(repo, relative: str, kind: str = "file") -> None:
    path = artifact_path(repo, relative, kind)
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif kind == "directory":
        if path.exists():
            shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def release_artifact(db, job, relative: str) -> None:
    """Remove only the registry entry; caller commits alongside publication."""
    job.payload = {**job.payload, "artifacts": [item for item in registered(job)
        if not isinstance(item, dict) or item.get("path") != relative]}


def record_artifact(repo, job_id: str, relative: str, kind: str = "file") -> None:
    path = artifact_path(repo, relative, kind)
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise DomainError("NOT_FOUND", "产物所属任务不存在。", 404)
        existing = registered(job)
        if any(isinstance(item, dict) and item.get("path") == relative and item.get("kind") == kind for item in existing):
            return
        if path.exists() or path.is_symlink():
            raise DomainError("ARTIFACT_EXISTS", "产物位置已存在，请使用新的任务文件名。", 409)
        for other in db.scalars(select(Job).where(Job.id != job_id)):
            if any(isinstance(item, dict) and item.get("path") == relative for item in registered(other)):
                raise DomainError("ARTIFACT_OWNED", "产物位置已属于其他任务。", 409)
        job.payload = {**job.payload, "artifacts": [*existing, {"path": relative, "kind": kind}]}


@contextmanager
def artifact_workspace(repo, job_id: str):
    relative = "incoming/work-" + uid()
    record_artifact(repo, job_id, relative, kind="directory")
    path = artifact_path(repo, relative, "directory")
    created = False
    try:
        path.mkdir()
        created = True
        yield path
    finally:
        cleaned = not created
        if created:
            try:
                remove_artifact(repo, relative, "directory")
                cleaned = True
            except (OSError, DomainError):
                # Persist ownership for a later recovery; do not hide the original
                # task exception or claim that an incomplete cleanup succeeded.
                pass
        if cleaned:
            try:
                with repo.transaction() as db:
                    job = db.get(Job, job_id)
                    if job is not None:
                        release_artifact(db, job, relative)
            except (OSError, SQLAlchemyError):
                # A stale manifest is safe to retry. Cleanup bookkeeping must
                # not turn an already committed published result into failure.
                pass


def protected_output_references(repo, db) -> set[str]:
    protected = set()
    for version in db.scalars(select(Version)):
        for value in (version.output_path, version.narration_path):
            relative = output_reference(repo, value)
            if relative:
                protected.add(relative)
    for job in db.scalars(select(Job)):
        steps = job.payload.get("steps", {})
        tts = steps.get("tts", {}) if isinstance(steps, dict) else {}
        relative = output_reference(repo, tts.get("path") if isinstance(tts, dict) else None)
        if relative:
            protected.add(relative)
        if job.state == "running":
            for item in registered(job):
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    protected.add(item["path"])
    return protected


def recover_artifacts(repo) -> ArtifactRecovery:
    """Call after the worker lease and repo.recover(); clean only owned scratch.

    Published Version files and paid TTS checkpoints survive. Running tasks and
    all unregistered paths are untouched. Failures keep their registry entries.
    """
    attempted = pending = retained = 0
    with repo.transaction() as db:
        protected = protected_output_references(repo, db)
        for job in db.scalars(select(Job).where(Job.state != "running")):
            for item in list(registered(job)):
                if not isinstance(item, dict):
                    pending += 1
                    continue
                relative, kind = item.get("path"), item.get("kind")
                if isinstance(relative, str) and relative in protected:
                    retained += 1
                    continue
                attempted += 1
                try:
                    remove_artifact(repo, relative, kind)
                except (OSError, DomainError, TypeError):
                    pending += 1
                else:
                    release_artifact(db, job, relative)
    return ArtifactRecovery(attempted=attempted, pending=pending, retained=retained)
