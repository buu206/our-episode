"""Operator-only Pro text comparison; never selected by production routing.

Same scoped source consent and transport, different audited model/tariff.
No tool execution, no images, no global configuration changes.
"""
from .director_chat_access import ScopedDirectorChat, _stop
from . import sequence_review, story_evidence
from ..models import Episode, Version


class ProTextComparison(ScopedDirectorChat):
    transport_model = 'deepseek-v4-pro'
    response_models = {'deepseek-v4-pro', 'DeepSeek-V4-Pro-0813', 'deepseek-v4-pro-0813'}
    token_rates = ('.30', '9', '27')
    pricing_revision = '2026-11-17'
    # 32000 * 9 + 2048 * 27 = 0.343296 yuan at peak, rounded up.
    required_reservation_yuan = .35
    input_limit = 32000  # Independent experimental cap; do not inherit Flash growth.

    def _validate_payload(self):
        super()._validate_payload()
        with self._repo.factory() as db:
            ep = self._repo.owned(db, Episode, self._context['episode']['id'], 'local-owner')
            current = self._context.get('current_version')
            version = self._repo.owned(db, Version, current['id'], ep.owner_id) if current else None
            if (self._context.get('sequence_check') != sequence_review.context(self._repo, db, version)
                    or self._context.get('story_evidence') != story_evidence.context(ep, version)):
                _stop('DIRECTOR_CONTEXT_CHANGED', '对照所用故事证据已变化，尚未发送请求。')
