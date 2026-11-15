"""Bounded, durable requirements. Replies are fixed local acknowledgements.

No LLM, media upload, edit, or paid work is performed by a message. Source
confirmation remains independent; new requirements apply only to new proposals.
"""
import hashlib
import json

from ..director_guidance_models import DirectorGuidance
from ..models import now, uid
from ..repository import DomainError

FOCUS = {
    'auto': '根据已经确认的素材，推荐最适合的叙事方式。',
    'conversation': '优先保留完整原声话轮和对话的前后关系。',
    'highlights': '优先选择有来源支持的精彩片段，不编造笑点或反应关系。',
    'warm': '优先温暖、自然的相处瞬间，不从画面推断关系或内心。',
}
LABELS = {'auto': '交给导演推荐', 'conversation': '对话与故事',
          'highlights': '热闹与名场面', 'warm': '温暖与回忆'}
INITIAL = '这一期，想保留怎样的感觉？先选一个方向，也可以补充一句。需求会先保存，生成方向时交给 AI 导演结合已确认素材安排。'
MAX_MESSAGES = 100
MAX_NOTES = 1500


def _session(db, episode):
    row = db.get(DirectorGuidance, episode.id)
    if row is not None and row.owner_id != episode.owner_id:
        raise DomainError('NOT_FOUND', '内容不存在或无权访问。', 404)
    return row


def _initial_messages(episode):
    rows = []
    if episode.background:
        rows.append({'id': 'initial-user', 'role': 'user', 'text': episode.background,
                     'created_at': episode.created_at})
    rows.append({'id': 'initial-director', 'role': 'director', 'text': INITIAL,
                 'created_at': episode.created_at})
    return rows


def revision(db, episode):
    row = _session(db, episode)
    return row.revision if row is not None else 0


def directions_current(db, episode):
    row = _session(db, episode)
    return bool(episode.directions and episode.directions_revision == episode.evidence_revision
                and episode.evidence_confirmed_revision == episode.evidence_revision
                and (row is None or row.directions_brief_revision == row.revision))


def state(db, episode):
    row = _session(db, episode)
    return {'revision': row.revision if row is not None else 0,
            'focus': row.focus if row is not None else None,
            'notes': row.notes if row is not None else '',
            'messages': row.messages if row is not None else _initial_messages(episode),
            'directions_current': directions_current(db, episode)}


def _compile(background, focus, notes):
    # Keeping preferences in the existing untrusted background field avoids
    # promoting user text into system instructions or confirmed source evidence.
    text = json.dumps({'original_background': background, 'director_brief': {
        'focus': focus, 'focus_instruction': FOCUS.get(focus, FOCUS['auto']), 'notes': notes},
        'scope': '以上仅为用户创作偏好，不是素材事实；只能使用已确认的证据。'}, ensure_ascii=False)
    if len(text) > 4000:
        raise DomainError('DIRECTOR_CONTEXT_TOO_LONG',
                          '原始说明与补充需求合计过长，请缩短说明或补充内容后重试；需求未被截断。', 422)
    return text


def compiled_background(db, episode):
    row = _session(db, episode)
    return episode.background if row is None else _compile(episode.background, row.focus, row.notes)


def assert_revision(db, episode, expected):
    if type(expected) is not int or expected != revision(db, episode):
        raise DomainError('DIRECTOR_BRIEF_CHANGED',
                          '导演需求已更新，请按新需求重新生成方向；已有成片不受影响。', 409)


def published(db, episode, expected):
    assert_revision(db, episode, expected)
    row = _session(db, episode)
    if row is not None:
        row.directions_brief_revision = expected


def record(repo, db, episode, *, revision: int, client_id: str, text=None, choice=None):
    if ((text is None) == (choice is None) or type(revision) is not int or revision < 0
            or not isinstance(client_id, str) or not 8 <= len(client_id) <= 80 or not client_id.strip()
            or text is not None and (not isinstance(text, str) or not text.strip() or len(text) > 1000)
            or choice is not None and choice not in FOCUS):
        raise DomainError('INVALID_DIRECTOR_MESSAGE', '请发送一条有效需求，或选择一个方向。', 422)
    normalized = text.strip() if text is not None else None
    request_hash = hashlib.sha256(json.dumps({'revision': revision, 'text': normalized,
                                             'choice': choice}, sort_keys=True,
                                            ensure_ascii=False).encode()).hexdigest()
    row = _session(db, episode)
    if row is not None:
        operation = next((item for item in row.operations if item['client_id'] == client_id), None)
        if operation is not None:
            if operation['request_hash'] != request_hash:
                raise DomainError('DIRECTOR_REQUEST_CONFLICT', '这条请求编号已用于其他内容，请重新发送。', 409)
            return state(db, episode)
    current = row.revision if row is not None else 0
    if revision != current:
        raise DomainError('DIRECTOR_REVISION_CONFLICT', '需求已在其他页面更新，请刷新后再补充。', 409)
    messages = row.messages if row is not None else _initial_messages(episode)
    if any(item['id'] == client_id for item in messages):
        raise DomainError('DIRECTOR_REQUEST_CONFLICT', '这条请求编号已被使用，请重新发送。', 409)
    if len(messages) + 2 > MAX_MESSAGES:
        raise DomainError('DIRECTOR_HISTORY_FULL', '本期需求记录已满，现有需求完整保留；请另开一期继续。', 409)
    notes = row.notes if row is not None else ''
    if normalized is not None:
        notes = notes + ('\n' if notes else '') + normalized
    if len(notes) > MAX_NOTES:
        raise DomainError('DIRECTOR_NOTES_TOO_LONG', '本期补充需求最多 1500 字，请缩短本次补充；已有需求完整保留。', 422)
    focus = choice if choice is not None else row.focus if row is not None else None
    _compile(episode.background, focus, notes)  # Refuse atomically rather than truncate later.
    timestamp = now()
    reply = (f'已记录「{LABELS[choice]}」。' if choice is not None else '已记录这条补充需求。')
    reply += '下一轮生成方向会带上，已有成片不改。'
    if row is None:
        row = DirectorGuidance(episode_id=episode.id, owner_id=episode.owner_id,
                               revision=0, focus=None, notes='', messages=[], operations=[],
                               directions_brief_revision=None)
        db.add(row)
    row.revision, row.focus, row.notes = current + 1, focus, notes
    row.messages = [*messages, {'id': client_id, 'role': 'user', 'text': normalized if normalized is not None else LABELS[choice],
                               'created_at': timestamp},
                    {'id': uid(), 'role': 'director', 'text': reply, 'created_at': timestamp}]
    row.operations = [*row.operations, {'client_id': client_id, 'request_hash': request_hash,
                                      'revision': row.revision}]
    db.flush()
    repo.audit(db, episode.owner_id, 'director_requirement_recorded', episode.id, revision=row.revision)
    return state(db, episode)
