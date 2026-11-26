#!/usr/bin/env python3
"""Compare one existing Flash chat with Pro, without publishing or applying it.

Default is a local preflight. --execute uses the existing three-clip consent and
3/30 yuan ledger, reserves at most .35 yuan, and never retries an existing run.
"""
import argparse
import hashlib
from pathlib import Path
import sys

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from sqlalchemy import select
from app.config import Settings
from app.chat_models import DirectorChatTurn
from app.models import Job
from app.repository import DomainError, Repository
from app.services import director_chat
from app.services.director import DirectorError
from app.services.director_chat_access import _digest
from app.services.director_chat_model import load_chat_prompt, prepare_chat_input
from app.services.director_text_comparison import ProTextComparison
from app.services.pipeline import ModelServices, Pipeline


def compare(repo, config, source_id, execute=False):
    with repo.factory() as db:
        source = repo.owned(db, Job, source_id, 'local-owner')
        if source.kind != 'director_chat' or source.state != 'succeeded':
            raise DomainError('SOURCE_NOT_READY', '需使用已完成的导演对话作为同题对照。')
        episode, assets = director_chat.validate_job(repo, db, source)
        turn = repo.owned(db, DirectorChatTurn, source.payload['turn_id'], source.owner_id)
        context = turn.context
        source_digest = _digest(context)
        prompt_digest = hashlib.sha256(load_chat_prompt(context).encode()).hexdigest()
        user, _ = prepare_chat_input(context)
        if len((load_chat_prompt(context) + user).encode()) + 2048 > ProTextComparison.input_limit:
            raise DomainError('INPUT_LIMIT', '当前同题材料超过对照的费用预留上限。')
        payload = {'asset_ids': [a.id for a in assets], 'revision': episode.evidence_revision,
                   'source_job': source_id, 'context_digest': source_digest, 'prompt_digest': prompt_digest,
                   'comparison_model': ProTextComparison.transport_model,
                   'input_limit': ProTextComparison.input_limit,
                   'reservation_yuan': ProTextComparison.required_reservation_yuan}
        owner, eid, vid = source.owner_id, source.episode_id, source.version_id

    class Services(ModelServices):
        def _chat_adapter(self, context=None, permit=None):
            return ProTextComparison(self.config, repo, context, permit)

        def profile(self):
            return {**super().profile(), 'comparison_model': ProTextComparison.transport_model,
                    'comparison_mode': 'text-only-no-publication'}

    class ComparisonPipeline(Pipeline):
        def _validate_job(self, db, job):
            result = super()._validate_job(db, job)
            original = repo.owned(db, Job, source_id, owner)
            director_chat.validate_job(repo, db, original)
            saved_turn = repo.owned(db, DirectorChatTurn, original.payload['turn_id'], owner)
            if (_digest(saved_turn.context) != source_digest
                    or hashlib.sha256(load_chat_prompt(context).encode()).hexdigest() != prompt_digest):
                raise DomainError('CONTEXT_CHANGED', '同题对照依据发生变化，未重提。')
            return result

    services = Services(config)
    services.repo = repo
    pipeline = ComparisonPipeline(repo, services)
    pipeline.require_ready('director_chat', context)
    key = 'director-pro-comparison:' + _digest(payload)
    with repo.transaction() as db:
        previous = db.scalars(select(Job).where(Job.owner_id == owner, Job.idempotency_key == key)).first()
        if previous:
            return {'job': previous.id, 'state': previous.state, 'reused': True, 'new_calls': 0}
        if not execute:
            return {'mode': 'preflight', 'model': ProTextComparison.transport_model,
                    'reservation_yuan': ProTextComparison.required_reservation_yuan, 'budget': repo.budget(db, eid), 'new_calls': 0}
        # Committed as running, never placed in the web worker queue.
        job = Job(owner_id=owner, episode_id=eid, version_id=vid, kind='director_comparison',
                  state='running', idempotency_key=key, payload=payload)
        db.add(job)
        db.flush()
        jid = job.id
        pipeline._validate_job(db, job)
    try:
        result = pipeline.invoke(jid, 'director_chat', ProTextComparison.required_reservation_yuan,
            lambda reservation, permit: services.director_chat(context, reservation, permit),
            request_digest=_digest({'context': source_digest, 'prompt': prompt_digest}),
            checkpoint_key='comparison', readiness_context=context)
        with repo.transaction() as db:
            job = db.get(Job, jid)
            pipeline._validate_job(db, job)
            job.state = 'succeeded'
            job.result = {'mode': 'text-only-no-publication', 'response': result.model_dump(),
                          'quality': 'not_accepted', 'source_job': source_id}
            repo.audit(db, owner, 'director_comparison_completed', jid)
        return {'job': jid, 'state': 'succeeded', 'quality': 'not_accepted', 'published': False}
    except (DomainError, DirectorError) as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.error = 'failed', {'code': exc.code, 'message': exc.message}
        return {'job': jid, 'state': 'failed', 'error': exc.code, 'published': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-job', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    import json
    try:
        settings = Settings.from_env()
        if not (settings.data_dir / 'app.sqlite3').is_file():
            raise ValueError('missing existing database')
        repo = Repository(settings.data_dir)
        try:
            print(json.dumps(compare(repo, settings.cloud, args.source_job, args.execute), ensure_ascii=False))
        finally:
            repo.engine.dispose()
    except (DirectorError, DomainError) as exc:
        print(json.dumps({'error': exc.code}, ensure_ascii=False))
        return 1
    except Exception:
        print('对照未完成，请检查本机配置；未输出密钥或供应商原始响应。')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
