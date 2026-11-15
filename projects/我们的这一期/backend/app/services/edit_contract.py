"""Persist the new contract in existing audit records; legacy versions remain readable."""
from sqlalchemy import select
from ..models import Audit
from ..repository import DomainError
from .editing import validate_director_segments

ACTION = 'director_edit_contract_v1'


def is_director_edit(db, version) -> bool:
    return db.scalars(select(Audit.id).where(Audit.target_id == version.id,
        Audit.owner_id == version.owner_id, Audit.action == ACTION).limit(1)).first() is not None


def check_edit(segments, episode) -> None:
    if episode.evidence_confirmed_revision != episode.evidence_revision:
        raise DomainError('EVIDENCE_CHANGED', '素材依据已更新，请先核对后再安排镜头。', 409)
    try:
        validate_director_segments(segments, episode.evidence)
    except (ValueError, TypeError, KeyError):
        raise DomainError('INVALID_DIRECTOR_EDIT', '镜头用途、来源依据或完整原声已失配，请保留完整话轮，并在无对白处使用转场。', 422) from None


def mark_edit(repo, db, version):
    repo.audit(db, version.owner_id, ACTION, version.id)
