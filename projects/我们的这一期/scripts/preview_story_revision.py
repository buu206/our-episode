#!/usr/bin/env python3
"""Operator-only revision rehearsal. Keeps the selected story and film intact.

Preflight is local; --execute reserves .28 yuan in the existing 3/30 yuan ledger.
Uses the production scoped adapter; results stay in a validation-only task.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from sqlalchemy import select
from app.config import Settings
from app.models import Episode, Job, Version
from app.repository import DomainError, Repository
from app.services import director_chat, director_guidance, revision_context
from app.services.director import DirectorError
from app.services.director_chat_access import ScopedDirector, _digest
from app.services.pipeline import ModelServices, Pipeline


def preview(repo, config, version_id, requirement, execute=False):
    if not requirement.strip() or len(requirement) > 1000:
        raise DomainError('INVALID_REQUIREMENT', '请提供1到1000字的具体改版要求。')
    with repo.factory() as db:
        version = repo.owned(db, Version, version_id, 'local-owner')
        ep = repo.owned(db, Episode, version.episode_id, version.owner_id)
        from app.services.sequence_review import context as sequence_context
        from app.services.story_evidence import context as speech_context
        saved, _, _ = director_chat.snapshot(repo, db, ep, version.id)
        # Replanning needs the cut and its evidence, not chat history, UI
        # capabilities, subtitle diagnostics or the chat-specific token cap.
        context = {'episode': {'id': ep.id}, 'question': requirement,
                   'current_version': director_chat._version_data(version),
                   'sequence_check': sequence_context(repo, db, version),
                   'story_evidence': speech_context(ep, version)}
        owner, eid = ep.owner_id, ep.id
        brief = director_guidance.revision(db, ep)
    reference = revision_context.snapshot(repo, context)
    adapter = ScopedDirector(config, repo, context, None)
    adapter._guard()
    adapter._revision_context = reference
    evidence, assets, background = adapter._current_payload()
    prompt = adapter._proposal_prompt()
    user = adapter._proposal_input(evidence, assets, background)
    if len((prompt + user).encode()) + 2048 > adapter.input_limit:
        raise DomainError('INPUT_LIMIT', '改版材料超过本轮费用预留范围。')
    prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
    key = 'story-revision-preview:' + _digest([saved, context, reference, prompt_sha])

    class Services(ModelServices):
        def ready(self):
            return ScopedDirector(self.config, repo, context, None).ready()

        def director_estimate(self):
            return .28

        async def directions(self, evidence, assets, background, reservation, permit):
            return await ScopedDirector(self.config, repo, context, permit).propose(
                evidence, assets, background, reservation=reservation)

        def profile(self):
            return {**super().profile(), 'rehearsal': 'story-revision-no-publication',
                    'context_digest': _digest(context), 'prompt_sha256': prompt_sha}

    class RehearsalPipeline(Pipeline):
        def _validate_job(self, db, job):
            result = super()._validate_job(db, job)
            director_chat.require_snapshot(repo, db, result[0], saved)
            if _digest(revision_context.snapshot(repo, context)) != _digest(reference):
                raise DomainError('REVISION_CONTEXT_CHANGED', '改版依据已改变，未重新发送。')
            current_prompt = adapter._proposal_prompt()
            if hashlib.sha256(current_prompt.encode()).hexdigest() != prompt_sha:
                raise DomainError('PROMPT_CHANGED', '改版提示已改变，请重新预检。')
            return result

    services = Services(config)
    services.repo = repo
    pipeline = RehearsalPipeline(repo, services)
    pipeline.require_ready()
    with repo.transaction() as db:
        previous = db.scalars(select(Job).where(Job.owner_id == owner, Job.idempotency_key == key)).first()
        if previous:
            return {'job': previous.id, 'state': previous.state, 'new_calls': 0}
        if not execute:
            return {'mode': 'preflight', 'reservation_yuan': .28, 'new_calls': 0,
                    'budget': repo.budget(db, eid), 'input_bytes': len((prompt + user).encode())}
        job = Job(owner_id=owner, episode_id=eid, version_id=version_id, kind='directions',
                  state='running', idempotency_key=key,
                  payload={'asset_ids': saved['asset_ids'], 'revision': saved['revision'],
                           'brief_revision': brief, 'validation_only': True,
                           'revision_context': context, 'reference_digest': _digest(reference),
                           'model_profile': services.profile()})
        db.add(job)
        db.flush()
        jid = job.id
        pipeline._validate_job(db, job)
    try:
        pipeline.directions(jid)
    except (DomainError, DirectorError) as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.error = 'failed', {'code': exc.code, 'message': exc.message}
        return {'job': jid, 'state': 'failed', 'error': exc.code, 'published': False}
    with repo.factory() as db:
        job = db.get(Job, jid)
        return {'job': jid, 'state': job.state, 'direction_count': job.result['direction_count'],
                'published': False, 'quality': 'not_accepted',
                'revision_review': job.result.get('revision_review', [])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--requirement', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if not (settings.data_dir / 'app.sqlite3').is_file():
            raise ValueError('missing existing data')
        repo = Repository(settings.data_dir)
        try:
            print(json.dumps(preview(repo, settings.cloud, args.version, args.requirement, args.execute), ensure_ascii=False))
        finally:
            repo.engine.dispose()
    except (DirectorError, DomainError) as exc:
        print(json.dumps({'error': exc.code}, ensure_ascii=False))
        return 1
    except Exception:
        print('改版预演未完成；未输出密钥或模型原始响应。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
