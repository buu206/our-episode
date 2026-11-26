"""Durable model dialogue. Suggestions have no effects until explicit acceptance."""
from copy import deepcopy
import hashlib
import json
from sqlalchemy import select
from ..chat_models import DirectorChatTurn
from ..models import Budget, Episode, Job, Version, uid
from ..repository import DomainError
from . import director_guidance

MAX_TURNS = 200  # Bounded stored dialogue; only the latest four turns enter model context.
CHAT_LIMIT_MESSAGE = f'本期导演对话已达到{MAX_TURNS}轮，已有记录完整保留。'
CONTEXT_BYTES = 23000  # Fits up to four observation windows plus a normal user question.


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _turns(db, episode):
    return list(db.scalars(select(DirectorChatTurn).where(
        DirectorChatTurn.episode_id == episode.id,
        DirectorChatTurn.owner_id == episode.owner_id).order_by(DirectorChatTurn.created_at, DirectorChatTurn.id)))


def _version(repo, db, episode, version_id):
    if not version_id:
        return None
    version = repo.owned(db, Version, version_id, episode.owner_id)
    if version.episode_id != episode.id:
        raise DomainError('NOT_FOUND', '内容不存在或无权访问。', 404)
    from ..worker import source_segments
    source_segments(repo, db, version)
    if version.state == 'restricted':
        raise DomainError('VERSION_UNAVAILABLE', '此版本来源已受限，先核对素材再继续。', 409)
    return version


def _version_data(version):
    if version is None:
        return None
    return {key: deepcopy(getattr(version, key)) for key in
            ('id', 'title', 'state', 'segments', 'narration_text', 'packaging')}


def snapshot(repo, db, episode, version_id):
    from .pipeline import active_assets
    assets = active_assets(db, episode)
    version = _version(repo, db, episode, version_id)
    return {'revision': episode.evidence_revision,
            'brief_revision': director_guidance.revision(db, episode),
            'analysis_state': episode.analysis_state,
            'confirmed_revision': episode.evidence_confirmed_revision,
            'episode_hash': _hash({'title': episode.title, 'background': episode.background,
                'selected_direction': episode.selected_direction, 'directions': episode.directions}),
            'asset_ids': sorted(a.id for a in assets),
            'version_id': version_id, 'version_hash': _hash(_version_data(version))}, assets, version


def require_snapshot(repo, db, episode, expected):
    current, assets, version = snapshot(repo, db, episode, expected.get('version_id'))
    if current != expected:
        raise DomainError('DIRECTOR_CONTEXT_CHANGED', '节目内容或要求已更新，请让导演按当前状态重新建议。', 409)
    return assets, version


def ready(services, context=None):
    try:
        if not hasattr(services, 'chat_ready'):
            return False, '当前服务尚未接入导演对话。'
        return services.chat_ready(context=context)
    except Exception:
        return False, '导演对话暂不可用，请检查当前模型配置。'


def require_ready(services, context=None):
    allowed, reason = ready(services, context)
    if not allowed:
        raise DomainError('DIRECTOR_CHAT_UNAVAILABLE', reason, 409)


def _context(repo, db, episode, rows, text, version_id, step, audio_focus_cue_id=None):
    saved, assets, version = snapshot(repo, db, episode, version_id)
    guidance = director_guidance.state(db, episode)
    confirmed = (episode.evidence_confirmed_revision == episode.evidence_revision
                 and episode.analysis_state == 'confirmed')
    evidence = deepcopy(episode.evidence) if confirmed else []
    available = ['navigate']
    if evidence:
        available.append('revise_directions')
    if version and version.packaging in ('standard', 'variety', 'variety_v2'):
        available.append('revise_narration')
    from .director_cut import eligible
    if eligible(db, version):
        available.append('revise_cut')
    history = []
    for row in rows[-4:]:
        history.append({'role': 'user', 'text': row.question})
        if row.reply is not None:
            history.append({'role': 'assistant', 'text': row.reply})
    omitted = sum(1 + int(row.reply is not None) for row in rows[:-4])
    context = {'episode': {'id': episode.id, 'title': episode.title,
        'background': episode.background, 'analysis_state': episode.analysis_state,
        'evidence_revision': episode.evidence_revision, 'brief_revision': saved['brief_revision'],
        'focus': guidance['focus'], 'requirements': guidance['notes']},
        'assets': [{'id': a.id, 'duration': a.duration, 'has_audio': a.has_audio} for a in assets],
        'confirmed_evidence': evidence, 'current_version': _version_data(version),
        'history': history, 'history_omitted_count': omitted, 'question': text,
        'step': step, 'available_actions': available}
    from .sequence_review import capability
    context['sequence_review_available'] = capability(repo, db, version)
    if context['sequence_review_available']:
        available.append('review_sequence')
    from .render_review import context as review_context
    context['output_check'] = review_context(repo, db, version)
    from .visual_review import context as visual_context
    context['visual_check'] = visual_context(repo, db, version)
    from .sequence_review import context as sequence_context
    sequence = sequence_context(repo, db, version)
    from .editorial_timing import context as editorial_context
    editorial = editorial_context(version, sequence)
    if sequence:
        context['sequence_check'] = sequence
        context['visual_check'] = None  # Prefer the scoped sequence over redundant broad screenshots.
    if sequence or editorial:
        # Preserve user intent, but don't feed obsolete assistant judgements back
        # as a precedent after fresh observations. Durable history is untouched.
        fresh_history = []
        for row in rows[-4:]:
            fresh_history.append({'role': 'user', 'text': row.question})
            if row.reply is not None:
                previous = getattr(row, 'context', None) or {}
                if previous.get('sequence_check') == sequence and previous.get('editorial_timing') == editorial:
                    fresh_history.append({'role': 'assistant', 'text': row.reply})
                else:
                    context['history_omitted_count'] += 1
        history[:] = fresh_history

    from .story_evidence import context as story_context
    context['story_evidence'] = story_context(episode, version)
    context['editorial_timing'] = editorial
    from .audio_review import context as audio_context
    context['audio_check'] = audio_context(repo, db, version, audio_focus_cue_id)
    if audio_focus_cue_id and not context['audio_check']:
        raise DomainError('AUDIO_FOCUS_STALE', '当前版本没有这句的有效检查，请回到预览重新选择。', 409)
    if context['audio_check'] and context['audio_check']['findings']:
        available.append('revise_subtitles')
    from .director_run import find as find_run, scope as run_scope
    run = find_run(db, version) if version else None
    context['local_delegation'] = ({'state': run.state, 'stage': run.stage,
        'scope': run_scope(run), 'cloud_calls_allowed': False,
        'stop_at': 'unconfirmed_preview', 'error': run.error} if run else None)
    from .story_events import derive as derive_events
    context['story_event_catalog'] = derive_events(context)
    # Only whole older conversation messages leave the input window. Durable
    # history, the current question and confirmed source evidence stay intact.
    from .director_chat_model import input_fits, prepare_chat_input
    from .director import DirectorError
    def fits():
        try:
            wire, _ = prepare_chat_input(context)
            return len(wire.encode()) <= CONTEXT_BYTES and input_fits(context)
        except DirectorError as exc:
            raise DomainError(exc.code, exc.message, 422) from None
    while not fits() and history:
        history.pop(0)
        context['history_omitted_count'] += 1
    if not fits():
        raise DomainError('DIRECTOR_CONTEXT_TOO_LONG', '本期已确认内容超出本轮对话上限，请先缩小需要讨论的素材范围。', 422)
    return saved, context


def enqueue(repo, db, episode, services, *, client_id, text, version_id=None, step='materials', confirm_send=False, audio_focus_cue_id=None):
    if not confirm_send:
        raise DomainError('CONFIRM_REQUIRED', '请确认把这句话及本期相关文字发送给导演模型。', 409)
    text = text.strip()
    if not text or len(text) > 2000 or not client_id.strip():
        raise DomainError('INVALID_CHAT_MESSAGE', '请填写1–2000字的问题。', 422)
    request_hash = _hash({'text': text, 'version_id': version_id, 'step': step} | (
        {'audio_focus_cue_id': audio_focus_cue_id} if audio_focus_cue_id else {}))
    rows = _turns(db, episode)
    previous = next((r for r in rows if r.client_id == client_id), None)
    if previous:
        if previous.request_hash != request_hash:
            raise DomainError('DIRECTOR_REQUEST_CONFLICT', '这个请求编号已用于另一句话，请刷新后重试。', 409)
        return repo.owned(db, Job, previous.job_id, episode.owner_id)
    require_ready(services, {'episode': {'id': episode.id}})
    if len(rows) >= MAX_TURNS:
        raise DomainError('DIRECTOR_CHAT_FULL', CHAT_LIMIT_MESSAGE, 409)
    if any(db.get(Job, row.job_id).state in ('queued', 'running') for row in rows):
        raise DomainError('DIRECTOR_CHAT_BUSY', '导演正在回复上一句话，请稍后再发。', 409)
    pending = db.scalars(select(Budget).where(Budget.episode_id == episode.id,
        Budget.owner_id == episode.owner_id,
        (Budget.state.in_(('reserved', 'unknown'))) | ((Budget.state == 'settled') & Budget.cost.is_(None)))).first()
    if pending:
        raise DomainError('COST_UNKNOWN', '本期尚有费用预留未核实，先等待处理或核对账单。', 409)
    saved, context = _context(repo, db, episode, rows, text, version_id, step, audio_focus_cue_id)
    turn_id = uid()
    job = Job(owner_id=episode.owner_id, episode_id=episode.id, kind='director_chat',
        idempotency_key='director_chat:' + turn_id,
        payload={'turn_id': turn_id, 'steps': {}})
    if 'revise_cut' in context['available_actions']:
        from .director_cut import binding
        job.payload = {**job.payload, 'cut_binding': binding(repo, db, repo.owned(db, Version, version_id, episode.owner_id))}
    if 'review_sequence' in context['available_actions']:
        from .sequence_review import binding
        job.payload = {**job.payload, 'sequence_binding': binding(repo, db, repo.owned(db, Version, version_id, episode.owner_id))}
    db.add(job); db.flush()
    db.add(DirectorChatTurn(id=turn_id, episode_id=episode.id, owner_id=episode.owner_id,
        client_id=client_id, request_hash=request_hash, question=text, reply=None,
        source='model' if services.is_real else 'demo', job_id=job.id,
        snapshot=saved, context=context, proposal=None))
    db.flush()
    repo.audit(db, episode.owner_id, 'director_chat_queued', job.id)
    return job



def require_sequence(repo, db, version, saved_context):
    if saved_context.get('sequence_check'):
        from .sequence_review import context as sequence_context
        if sequence_context(repo, db, version) != saved_context['sequence_check']:
            raise DomainError('SEQUENCE_REVIEW_STALE', '过程观察已变化，请按新结果重新讨论。', 409)


def validate_job(repo, db, job):
    episode = repo.owned(db, Episode, job.episode_id, job.owner_id)
    row = repo.owned(db, DirectorChatTurn, job.payload.get('turn_id'), job.owner_id)
    if row.job_id != job.id or row.episode_id != episode.id:
        raise DomainError('DIRECTOR_CONTEXT_CHANGED', '对话任务已失效。', 409)
    if _turns(db, episode)[-1].id != row.id:
        raise DomainError('DIRECTOR_CONTEXT_CHANGED', '已经开始新的对话，请在当前会话继续。', 409)
    assets, version = require_snapshot(repo, db, episode, row.snapshot)
    require_sequence(repo, db, version, row.context)
    if 'editorial_timing' in row.context:
        from .editorial_timing import context as editorial_context
        if editorial_context(version, row.context.get('sequence_check')) != row.context['editorial_timing']:
            raise DomainError('EDITORIAL_TIMING_STALE', '花字时间依据已变化，请按当前版本重新讨论。', 409)
    if 'review_sequence' in row.context.get('available_actions', []):
        from .sequence_review import require_binding
        require_binding(repo, db, version, job.payload.get('sequence_binding'))
    if 'revise_subtitles' in row.context.get('available_actions', []):
        from .audio_review import context as audio_context
        if audio_context(repo, db, version, (row.context.get('audio_check') or {}).get('focus_cue_id')) != row.context.get('audio_check'):
            raise DomainError('SUBTITLE_PROPOSAL_STALE', '对白检查已变化，未按过期结果继续建议。', 409)
    if 'revise_cut' in row.context.get('available_actions', []):
        from .director_cut import require_binding
        require_binding(repo, db, version, job.payload.get('cut_binding'))
    return episode, assets


def state(repo, db, episode, services):
    rows = _turns(db, episode)
    messages, proposals, pending = [], [], None
    for row in rows:
        messages.append({'id': row.id + '-user', 'role': 'user', 'text': row.question,
                         'source': 'user', 'client_id': row.client_id, 'created_at': row.created_at})
        if row.reply is not None:
            messages.append({'id': row.id + '-assistant', 'role': 'assistant', 'text': row.reply,
                             'source': row.source, 'created_at': row.created_at})
        job = db.get(Job, row.job_id)
        if job and job.state in ('queued', 'running'):
            pending = job.id
        if row.proposal:
            status = row.proposal['status']
            cut_description = None
            if status == 'pending':
                try:
                    _, current_version = require_snapshot(repo, db, episode, row.snapshot)
                    require_sequence(repo, db, current_version, row.context)
                    if row.proposal['kind'] == 'review_sequence':
                        from .sequence_review import require_binding
                        require_binding(repo, db, current_version, row.proposal.get('sequence_binding'))
                    if row.proposal['kind'] == 'revise_cut':
                        from .director_cut import resolve
                        selected, _ = resolve(repo, db, current_version, row.proposal['selections'], row.proposal['cut_binding'], row.context)
                        from .revision_review import describe_cut
                        cut_description = describe_cut(current_version.segments, selected, episode.evidence)
                    if row.proposal['kind'] == 'revise_subtitles':
                        from .director_subtitles import resolve
                        resolve(repo, db, current_version, row.context, row.proposal['corrections'], row.proposal['audio_snapshot'])
                except DomainError:
                    status = 'stale'
            item = {key: row.proposal[key] for key in
                    ('kind', 'title', 'explanation', 'requirement', 'narration_text', 'target_step', 'subtitle_changes', 'cut_changes', 'before_seconds', 'after_seconds', 'start', 'end')
                    if key in row.proposal}
            if row.proposal['kind'] in ('revise_subtitles', 'revise_cut'):
                item['base_version_id'] = row.snapshot['version_id']
            if cut_description is not None and status == 'pending':
                item['explanation'] = cut_description[0]
                if row.reply is not None:
                    messages[-1]['text'] = cut_description[1]
            if row.proposal['kind'] == 'review_sequence' and status == 'applied':
                from .sequence_review import proposal_status
                try:
                    version = repo.owned(db, Version, row.snapshot['version_id'], episode.owner_id)
                    item.update(proposal_status(repo, db, version, row.proposal))
                except DomainError:
                    item.update(review_state='stale', review_summary='此版本已不可用。')
            proposals.append({**item, 'id': row.id, 'message_id': row.id + '-assistant', 'status': status})
    last = db.get(Job, rows[-1].job_id) if rows else None
    allowed, reason = ready(services, {'episode': {'id': episode.id}})
    if len(rows) >= MAX_TURNS:
        allowed, reason = False, CHAT_LIMIT_MESSAGE
    return {'messages': messages, 'proposals': proposals, 'pending_job_id': pending,
            'last_job_id': last.id if last else None, 'last_error': last.error if last else None,
            'can_chat': allowed, 'reason': reason, 'mode': 'cloud' if services.is_real else 'demo'}


def run(pipeline, job_id):
    repo = pipeline.repo
    with repo.factory() as db:
        job = db.get(Job, job_id)
        validate_job(repo, db, job)
        turn = db.get(DirectorChatTurn, job.payload['turn_id'])
        context = deepcopy(turn.context)
    pipeline.stage(job_id, '导演正在结合本期内容回答')
    result = pipeline.invoke(job_id, 'director_chat', pipeline.services.chat_estimate(context),
        lambda reservation, permit: pipeline.services.director_chat(context, reservation, permit),
        checkpoint_key='director_chat', readiness_context=context)
    # Revalidate even injected adapters at the publication boundary.
    from .director_chat_model import ChatReply
    try:
        data = result.model_dump(exclude_none=True) if hasattr(result, 'model_dump') else result
        parsed = ChatReply.model_validate(data)
        data = parsed.model_dump(exclude_none=True)
        if data.get('action') and data['action']['kind'] not in context['available_actions']:
            raise ValueError('unavailable action')
    except (ValueError, TypeError):
        raise DomainError('INVALID_CHAT_REPLY', '导演回复结构不完整，请查看费用后重试。', 409) from None
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        validate_job(repo, db, job)
        turn = db.get(DirectorChatTurn, job.payload['turn_id'])
        suggestion_check = 'not_evaluated'
        action = data.get('action')
        event_review = None
        if action and action['kind'] == 'revise_cut' and action.get('event_decisions'):
            from .story_events import compile_decisions
            selections, event_review = compile_decisions(context, action['event_decisions'])
            action.pop('event_decisions')
            action['selections'] = selections
        if action and action['kind'] == 'review_sequence' and any(
                row['window'] == {k: action[k] for k in ('start', 'end')}
                for row in (context.get('sequence_check') or {}).get('windows', [])):
            # Repeating the same valid sampling cannot fill a continuous-motion
            # gap. Publish a clearly identified local failure, not a paid card
            # or the unsupported model claim that we have not looked here.
            suggestion_check = 'duplicate_review_rejected'
            data = {'reply': '这条建议没有通过检查：它让我重复查看已经观察过的同一段，不能补充新的依据。\n\n当前还不能据此确定剪点，原片先保留。这次没有修改，也没有再次发起动作观察。'}
            repo.audit(db, job.owner_id, 'director_suggestion_rejected', turn.id,
                       reason=suggestion_check)
        turn.reply = data['reply']
        turn.proposal = {**data['action'], 'status': 'pending'} if data.get('action') else None
        if turn.proposal and turn.proposal['kind'] == 'review_sequence':
            from .sequence_review import snapshot as review_snapshot, require_binding
            version = repo.owned(db, Version, turn.snapshot['version_id'], job.owner_id)
            bound = job.payload['sequence_binding']
            require_binding(repo, db, version, bound)
            review_snapshot(repo, db, version, {k: turn.proposal[k] for k in ('start', 'end')})
            turn.proposal = {**turn.proposal, 'sequence_binding': bound}
        if turn.proposal and turn.proposal['kind'] == 'revise_subtitles':
            from .director_subtitles import resolve
            version = repo.owned(db, Version, turn.snapshot['version_id'], job.owner_id)
            _, changes, bound = resolve(repo, db, version, turn.context, turn.proposal['corrections'])
            turn.proposal = {**turn.proposal, 'subtitle_changes': changes, 'audio_snapshot': bound}
        if turn.proposal and turn.proposal['kind'] == 'revise_cut':
            from .director_cut import resolve
            version = repo.owned(db, Version, turn.snapshot['version_id'], job.owner_id)
            selected, public = resolve(repo, db, version, turn.proposal['selections'], job.payload['cut_binding'], turn.context)
            from .revision_review import describe_cut
            episode = repo.owned(db, Episode, version.episode_id, job.owner_id)
            explanation, turn.reply, cut_review = describe_cut(version.segments, selected, episode.evidence)
            turn.proposal = {**turn.proposal, **public, 'explanation': explanation,
                             'cut_review': cut_review, 'cut_binding': job.payload['cut_binding'],
                             **({'event_review': event_review} if event_review else {})}
            suggestion_check = 'cut_description_from_plan'
        pipeline.finish(db, job, {'message_id': turn.id + '-assistant',
            'suggestion_check': suggestion_check},
            '重复观察建议已拦截，未改片' if suggestion_check == 'duplicate_review_rejected'
            else '导演已回复，修改建议需你确认')


def scoped_direction_context(repo, db, job):
    """Only a concretely accepted chat proposal can use the scoped adapter.

    The accepted requirement increments the brief revision. Rebuild that part
    from current storage; normal direction-job guards enforce its saved revision.
    """
    episode = repo.owned(db, Episode, job.episode_id, job.owner_id)
    turn = repo.owned(db, DirectorChatTurn, job.payload.get('chat_proposal_id'), job.owner_id)
    proposal = turn.proposal or {}
    if (turn.episode_id != episode.id or proposal.get('kind') != 'revise_directions'
            or proposal.get('status') != 'applied'
            or proposal.get('result', {}).get('job_id') != job.id):
        raise DomainError('DIRECTOR_CONTEXT_CHANGED', '导演建议与此任务不匹配，未重新发送。', 409)
    guidance = director_guidance.state(db, episode)
    context = deepcopy(turn.context)
    context['episode'].update(background=director_guidance.compiled_background(db, episode),
        analysis_state=episode.analysis_state, evidence_revision=episode.evidence_revision,
        brief_revision=guidance['revision'], focus=guidance['focus'], requirements=guidance['notes'])
    context['confirmed_evidence'] = deepcopy(episode.evidence)
    context['question'] = proposal['requirement']
    return context


def apply(repo, db, episode, proposal_id, services, enqueue_directions, confirm=False):
    if not confirm:
        raise DomainError('CONFIRM_REQUIRED', '请先确认导演建议的具体修改。', 409)
    turn = repo.owned(db, DirectorChatTurn, proposal_id, episode.owner_id)
    if turn.episode_id != episode.id or not turn.proposal:
        raise DomainError('NOT_FOUND', '建议不存在或无权访问。', 404)
    proposal = turn.proposal
    if proposal['status'] == 'applied':
        return proposal['result']
    _, version = require_snapshot(repo, db, episode, turn.snapshot)
    require_sequence(repo, db, version, turn.context)
    kind = proposal['kind']
    result = {'ok': True}
    if kind == 'navigate':
        result['next_step'] = proposal['target_step']
        if version:
            result['version_id'] = version.id
    elif kind == 'revise_directions':
        from .pipeline import Pipeline, assert_sources
        Pipeline(repo, services).require_ready('directions', turn.context)
        if episode.evidence_confirmed_revision != episode.evidence_revision or not episode.evidence:
            raise DomainError('REVIEW_REQUIRED', '请先确认素材理解，再让导演重做方案。', 409)
        assert_sources(db, episode, episode.analysis_asset_ids)
        # This transaction also encloses enqueue. A rejected job cannot silently
        # save only half of the user's accepted operation.
        director_guidance.record(repo, db, episode,
            revision=director_guidance.revision(db, episode),
            client_id='chat-action-' + turn.id, text=proposal['requirement'])
        job = enqueue_directions(db, repo, episode, 'directions')
        job.payload = {**job.payload, 'chat_proposal_id': turn.id}
        result.update(next_step='direction', job_id=job.id)
    elif kind == 'review_sequence':
        from .sequence_review import enqueue as enqueue_review, require_binding
        require_binding(repo, db, version, proposal.get('sequence_binding'))
        job = enqueue_review(repo, db, version, services, {k: proposal[k] for k in ('start', 'end')})
        result.update(job_id=job.id, version_id=version.id)
    elif kind == 'revise_cut':
        from .director_cut import create_candidate
        result = create_candidate(repo, db, episode, version, turn)
    elif kind == 'revise_subtitles':
        from .director_subtitles import create_candidate
        result = create_candidate(repo, db, episode, version, turn)
    elif kind == 'revise_narration':
        if not version or version.packaging not in ('standard', 'variety', 'variety_v2'):
            raise DomainError('VERSION_UNAVAILABLE', '当前版本不能直接使用旁白，请先选择适合的版本。', 409)
        from .edit_contract import is_director_edit, mark_edit
        new_version = Version(owner_id=episode.owner_id, episode_id=episode.id,
            title=(version.title + ' · 旁白调整')[:120], origin=version.origin,
            segments=deepcopy(version.segments), base_version_id=version.id,
            evidence_revision=version.evidence_revision, direction_id=version.direction_id,
            narration_text=proposal['narration_text'], packaging=version.packaging)
        db.add(new_version); db.flush()
        if is_director_edit(db, version):
            mark_edit(repo, db, new_version)
        result.update(next_step='preview', version_id=new_version.id)
    else:
        raise DomainError('INVALID_DIRECTOR_ACTION', '此建议暂不支持执行。', 422)
    turn.proposal = {**proposal, 'status': 'applied', 'result': result}
    repo.audit(db, episode.owner_id, 'director_action_applied', episode.id, kind=kind)
    return result
