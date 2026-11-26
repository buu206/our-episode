"""Carry a reviewed version into text replanning without upgrading observations."""
import json
from pathlib import Path
from ..models import Episode, Version
from ..repository import DomainError
from .director import prepare_input

PROMPT = Path(__file__).with_name('prompts') / 'director_revision.txt'


def snapshot(repo, context):
    current = context.get('current_version')
    if current is None:
        return None
    from .director_chat import _version_data
    from .sequence_review import context as sequence_context
    from .story_evidence import context as speech_context
    from .render_review import validate, fingerprint
    from .editing import timeline
    from ..worker import source_segments
    with repo.factory() as db:
        ep = repo.owned(db, Episode, context['episode']['id'], 'local-owner')
        version = repo.owned(db, Version, current['id'], ep.owner_id)
        if version.episode_id != ep.id or _version_data(version) != current:
            raise DomainError('REVISION_CONTEXT_CHANGED', '基准版本已变化，请按当前版本重新安排故事。', 409)
        source_segments(repo, db, version)
        path = validate(repo, db, version)
        observations = context.get('sequence_check')
        if observations is not None and observations != sequence_context(repo, db, version):
            raise DomainError('SEQUENCE_REVIEW_STALE', '故事改版所用的动作观察已更新，请重新讨论。', 409)
        speech = speech_context(ep, version)
        if 'story_evidence' in context and speech != context['story_evidence']:
            raise DomainError('REVISION_CONTEXT_CHANGED', '故事改版所用的对白位置已变化。', 409)
        return {'output_sha256': fingerprint(path), 'current_version': current,
                'current_output_timeline': [{'shot': i + 1, 'start': t.start, 'end': t.end}
                    for i, t in enumerate(timeline(version.segments))],
                'confirmed_speech_mapping': speech, 'unverified_sequence_observations': observations,
                'requested_change': context.get('question', ''),
                'scope': 'revision_reference_only', 'audio_reheard': False,
                'observation_times': 'current_output_seconds', 'segment_times': 'source_seconds'}


def prepare(evidence, assets, background, revision):
    original = prepare_input(evidence, assets, background)
    if revision is None:
        return original
    data = json.loads(original)
    data['untrusted_revision_context'] = revision
    return json.dumps(data, ensure_ascii=False, allow_nan=False)
