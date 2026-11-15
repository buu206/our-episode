"""Revoke access first; delete exact owned copies after in-flight work stops.

No directory sweeps, provider deletion, budget reset, or automatic enrollment.
The timer policy is separate from the deletion mechanism.
"""
import time
from pathlib import Path

from sqlalchemy import delete, select

from ..models import Asset, Audit, Episode, Job, ModelCall, Version
from ..collaboration_models import (Adoption, ExpressionRevision, Invitation, Objection,
                                    Participant, ParticipantSession, Submission)
from ..sharing_models import Share, ShareOperation, ViewerSession
from ..retention_models import Retention
from ..director_guidance_models import DirectorGuidance
from ..chat_models import DirectorChatTurn
from ..repository import DomainError
from .artifacts import artifact_path, output_reference, registered, remove_artifact
from .cleanup import _application_path


def unavailable(db, episode_id, *, at=None):
    row = db.get(Retention, episode_id)
    return bool(row and (row.requested_at is not None or (
        row.expires_at is not None and row.expires_at <= (time.time() if at is None else at))))


def enroll_existing(repo):
    """One-time grace for each pre-policy episode; restarts never renew it."""
    with repo.transaction() as db:
        deadline = time.time() + 30 * 86400
        for episode in db.scalars(select(Episode)):
            if db.get(Retention, episode.id) is None:
                db.add(Retention(episode_id=episode.id, owner_id=episode.owner_id, expires_at=deadline))


def require_available(db, episode_id):
    if unavailable(db, episode_id):
        raise DomainError('EPISODE_EXPIRED', '本期已到保存期限或已请求删除，内容不可访问。', 410)


def status(db, episode):
    row = db.get(Retention, episode.id)
    return {'expires_at': row.expires_at if row else None,
            'deletion_requested': bool(row and row.requested_at is not None),
            'cleanup_pending': bool(row and row.requested_at is not None and row.completed_at is None),
            'counts': {label: len(_rows(db, model, episode)) for label, model in (
                ('assets', Asset), ('versions', Version), ('submissions', Submission), ('shares', Share))}}


def request_deletion(repo, episode_id, owner_id, *, reason='manual', at=None):
    timestamp = time.time() if at is None else at
    with repo.transaction() as db:
        episode = db.get(Episode, episode_id)
        if not episode or episode.owner_id != owner_id:
            raise DomainError('NOT_FOUND', '内容不存在或无权访问。', 404)
        row = db.get(Retention, episode_id)
        if row is None:
            row = Retention(episode_id=episode_id, owner_id=owner_id)
            db.add(row)
        if row.requested_at is None:
            row.requested_at, row.reason = timestamp, reason
            repo.audit(db, owner_id, 'episode_deletion_requested', episode_id, reason=reason)
        for share in db.scalars(select(Share).where(Share.episode_id == episode_id, Share.owner_id == owner_id)):
            share.state, share.closed_at = 'closed', timestamp
        for invite in db.scalars(select(Invitation).where(Invitation.episode_id == episode_id, Invitation.owner_id == owner_id)):
            invite.closed_at = timestamp
        for job in db.scalars(select(Job).where(Job.episode_id == episode_id, Job.owner_id == owner_id, Job.state == 'queued')):
            job.state, job.stage = 'failed', '本期内容已请求删除'
            job.error = {'code': 'EPISODE_EXPIRED', 'message': '本期内容不可继续处理。'}


def _rows(db, model, episode):
    return list(db.scalars(select(model).where(model.episode_id == episode.id, model.owner_id == episode.owner_id)))


def _references(repo, db, episode):
    refs = set()
    def add(value, kind='file'):
        if value:
            # Keep invalid paths in the manifest, so failure never looks complete.
            refs.add((str(value), kind))
    for row in _rows(db, Asset, episode):
        add(row.path)
    for row in _rows(db, Version, episode):
        add(row.output_path); add(row.narration_path)
    for row in _rows(db, Submission, episode):
        add(row.source_path); add(row.preview_path)
    for row in _rows(db, Invitation, episode):
        add(row.context_path)
    for row in _rows(db, Adoption, episode):
        add(row.output_path)
    for row in _rows(db, Share, episode):
        add(row.output_path)
    for row in _rows(db, Job, episode):
        if row.kind in ('director_voice', 'director_voice_upload'):
            add(row.payload.get('voice_path'))
        for item in registered(row):
            add(item.get('path'), item.get('kind'))
        voice = row.payload.get('steps', {}).get('tts', {}).get('path')
        if voice:
            add(output_reference(repo, voice) or voice)
    return refs


def _remove(repo, relative, kind):
    if kind == 'directory':
        remove_artifact(repo, relative, kind)
        return
    parts = Path(relative).parts
    if kind != 'file' or len(parts) != 2 or parts[0] not in ('assets', 'outputs', 'collaboration'):
        raise ValueError('invalid deletion reference')
    _application_path(repo, relative, parts[0]).unlink(missing_ok=True)


def _clear_content(db, episode):
    """Keep only a tombstone and non-content billing facts; do not forgive costs."""
    target_ids = {episode.id}
    for model in (Asset, Version, Job, ModelCall, Submission, Invitation, Adoption, Objection, Share):
        target_ids.update(row.id for row in _rows(db, model, episode))
    submissions = _rows(db, Submission, episode)
    invitations = _rows(db, Invitation, episode)
    shares = _rows(db, Share, episode)
    invite_ids = [row.id for row in invitations]
    people = list(db.scalars(select(Participant).where(Participant.invite_id.in_(invite_ids))))
    target_ids.update(row.id for row in people)
    revisions = list(db.scalars(select(ExpressionRevision).where(
        ExpressionRevision.submission_id.in_([row.id for row in submissions]))))
    target_ids.update(row.id for row in revisions)
    db.execute(delete(ParticipantSession).where(ParticipantSession.participant_id.in_([row.id for row in people])))
    db.execute(delete(ViewerSession).where(ViewerSession.share_id.in_([row.id for row in shares])))
    db.execute(delete(ShareOperation).where(ShareOperation.owner_id == episode.owner_id,
                                           ShareOperation.share_id.in_([row.id for row in shares])))
    for row in [*revisions, *people]:
        db.delete(row)
    for model in (Adoption, Objection, Submission, Invitation, Share, Asset, Version):
        for row in _rows(db, model, episode):
            db.delete(row)
    db.execute(delete(DirectorGuidance).where(DirectorGuidance.episode_id == episode.id,
                                            DirectorGuidance.owner_id == episode.owner_id))
    db.execute(delete(DirectorChatTurn).where(DirectorChatTurn.episode_id == episode.id,
                                            DirectorChatTurn.owner_id == episode.owner_id))
    for job in _rows(db, Job, episode):
        job.payload, job.result = {}, None
        job.state, job.stage = 'failed', '本期已删除'
        job.error = {'code': 'EPISODE_EXPIRED', 'message': '本期内容已删除。'}
    for call in _rows(db, ModelCall, episode):
        call.request_digest = None
        allowed = {'model', 'model_version', 'provider', 'input_tokens', 'output_tokens',
                   'cost_yuan', 'cost_status', 'request_id', 'retries', 'real_call',
                   'seconds', 'characters', 'pricing_revision', 'price_band', 'usage_basis',
                   'cache_hit_tokens', 'cache_miss_tokens'}
        original_usage = call.usage
        call.usage = {key: value for key, value in original_usage.items() if key in allowed}
        if isinstance(original_usage.get('requests'), list):
            call.usage = {**call.usage, 'requests': [
                {key: value for key, value in row.items() if key in allowed}
                for row in original_usage['requests'] if isinstance(row, dict)]}
    db.execute(delete(Audit).where(Audit.owner_id == episode.owner_id, Audit.target_id.in_(target_ids)))
    episode.title, episode.background = '已删除节目', ''
    episode.evidence, episode.directions = [], []
    episode.analysis_asset_ids, episode.analysis_notes, episode.analysis_coverage = [], [], []
    episode.selected_direction = episode.directions_revision = episode.evidence_confirmed_revision = None
    episode.analysis_state = 'deleted'


def purge(repo, episode_id):
    # Keep the write reservation throughout file removal to prevent another
    # request publishing a reference while this manifest is being processed.
    with repo.transaction() as db:
        row = db.get(Retention, episode_id)
        episode = db.get(Episode, episode_id)
        if not row or row.requested_at is None or row.completed_at is not None or not episode:
            return
        if any(job.state == 'running' for job in _rows(db, Job, episode)):
            return  # Includes cloud settlement, rendering and HTTP upload jobs.
        if not row.contents_cleared:
            refs = _references(repo, db, episode)
            row.files = [{'path': path, 'kind': kind} for path, kind in sorted(refs)]
            _clear_content(db, episode)
            row.contents_cleared = True
    # Commit the cleanup manifest BEFORE unlink; a crash can retry exact paths.
    with repo.transaction() as db:
        row = db.get(Retention, episode_id)
        if not row or not row.contents_cleared or row.completed_at is not None:
            return
        protected = set()
        for other in db.scalars(select(Episode).where(Episode.id != episode_id)):
            protected.update(path for path, _ in _references(repo, db, other))
        # Include other outstanding manifests after their business rows are gone.
        for other in db.scalars(select(Retention).where(Retention.episode_id != episode_id)):
            protected.update(item['path'] for item in other.files)
        pending = []
        for item in row.files:
            try:
                if item['path'] in protected:
                    raise ValueError('reference shared with another episode')
                _remove(repo, item['path'], item['kind'])
            except (OSError, ValueError, TypeError, DomainError):
                pending.append(item)
        row.files = pending
    # SQLite secure_delete clears freed cells; truncate WAL after readers finish.
    # Busy readers keep the operation pending, never falsely "fully deleted".
    with repo.engine.connect() as connection:
        checkpoint = connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)').first()
    if checkpoint and checkpoint[0] == 0:
        with repo.transaction() as db:
            row = db.get(Retention, episode_id)
            if not row.files:
                row.completed_at = time.time()


def maintain(repo):
    with repo.factory() as db:
        rows = list(db.scalars(select(Retention).where(Retention.completed_at.is_(None))))
    for row in rows:
        if row.requested_at is None and row.expires_at is not None and row.expires_at <= time.time():
            request_deletion(repo, row.episode_id, row.owner_id, reason='expired')
        if row.requested_at is not None or (row.expires_at is not None and row.expires_at <= time.time()):
            purge(repo, row.episode_id)
