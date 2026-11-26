"""Durable finishing decisions. Saving never renders, calls a model or confirms a film."""
from copy import deepcopy
from typing import Literal
from fastapi import Depends
from pydantic import Field
from sqlalchemy import select
from .models import Episode, Job, Version
from .repository import DomainError
from .schemas import Strict, VersionIn, VersionOut


class FinishingState(Strict):
    revision: int = Field(ge=0)
    step: int = Field(ge=1, le=3)
    completed: int = Field(ge=0, le=3)
    draft: VersionIn
    generated_version_id: str | None = None


class SaveFinishing(Strict):
    revision: int = Field(ge=0)
    action: Literal['save', 'next', 'back']
    draft: VersionIn


class CommitFinishing(Strict):
    revision: int = Field(ge=0)


def cut_identity(draft):
    """Only presentation is editable here; source/evidence/timing remain immutable."""
    data = deepcopy(draft.model_dump())
    data.pop('narration_text')
    for segment in data['segments']:
        for key in ('caption', 'subtitle_edits', 'frame_accents'):
            segment.pop(key, None)
        for beat in segment.get('visual_beats', []):
            for key in ('text', 'palette', 'position', 'sound', 'sound_delay'):
                beat.pop(key, None)
        if segment.get('editorial'):
            for key in ('text', 'detail', 'sound', 'sound_delay'):
                segment['editorial'].pop(key, None)
    return data


def layers(draft):
    text, sound, frames = [], [], []
    for s in draft.model_dump()['segments']:
        beats = s.get('visual_beats', [])
        editorial = s.get('editorial') or {}
        text.append([s.get('caption'), s.get('subtitle_edits'),
                     [[b.get(k) for k in ('text', 'palette', 'position')] for b in beats],
                     [editorial.get(k) for k in ('text', 'detail')]])
        sound.append([[[b.get(k) for k in ('sound', 'sound_delay')] for b in beats],
                      [editorial.get(k) for k in ('sound', 'sound_delay')]])
        frames.append(s.get('frame_accents'))
    return [text, sound, [draft.narration_text, frames]]


def register_finishing(app, session, repository, create_version_record, version_out):
    def load(db, repo, vid, owner):
        base = repo.owned(db, Version, vid, owner)
        episode = repo.owned(db, Episode, base.episode_id, owner)
        if base.state not in ('ready', 'confirmed') or base.error or base.packaging != 'variety_v2':
            raise DomainError('FINISHING_UNAVAILABLE', '请先生成可播放的综艺版本，再调整包装。', 409)
        from .worker import source_segments
        source_segments(repo, db, base)
        job = db.scalar(select(Job).where(Job.owner_id == owner, Job.idempotency_key == 'finishing:' + vid))
        draft = VersionIn(title=base.title, segments=base.segments, base_version_id=vid,
                          packaging=base.packaging, narration_text=base.narration_text)
        if job and job.payload['evidence_revision'] != episode.evidence_revision:
            raise DomainError('FINISHING_STALE', '素材理解已更新，请先生成新版本再调整包装。', 409)
        state = FinishingState.model_validate(job.result) if job else FinishingState(revision=0, step=1, completed=0, draft=draft)
        return base, episode, job, state, draft

    @app.get('/api/v1/versions/{version_id}/finishing', response_model=FinishingState)
    def get_finishing(version_id: str, auth=Depends(session), repo=Depends(repository)):
        with repo.transaction() as db:
            return load(db, repo, version_id, auth['user_id'])[3]

    @app.put('/api/v1/versions/{version_id}/finishing', response_model=FinishingState)
    def save_finishing(version_id: str, body: SaveFinishing, auth=Depends(session), repo=Depends(repository)):
        owner = auth['user_id']
        with repo.transaction() as db:
            base, episode, job, state, original = load(db, repo, version_id, owner)
            if state.revision != body.revision:
                raise DomainError('FINISHING_CONFLICT', '另一处已保存新选择，请重新读取后再修改。', 409)
            if cut_identity(body.draft) != cut_identity(original):
                raise DomainError('FINISHING_CUT_CHANGED', '三步包装保留当前选段；改变选段请使用片段编辑。', 422)
            if any(a != b for i, (a, b) in enumerate(zip(layers(state.draft), layers(body.draft)), 1) if i != state.step):
                raise DomainError('FINISHING_WRONG_STEP', '请返回对应步骤修改，后续选择会继续保留。', 422)
            # Reuse all existing ownership, fixed-expression, subtitle and source guards.
            # A savepoint performs validation without creating a version or audit row.
            nested = db.begin_nested()
            try:
                create_version_record(db, repo, base.episode_id, body.draft, owner)
            finally:
                nested.rollback()
            changed = body.draft != state.draft
            if changed:
                state.completed = min(state.completed, state.step - 1)
                state.generated_version_id = None
            state.draft = body.draft
            if body.action == 'next':
                state.completed = max(state.completed, state.step)
                state.step = min(3, state.step + 1)
            elif body.action == 'back':
                state.step = max(1, state.step - 1)
            state.revision += 1
            if job is None:
                job = Job(owner_id=owner, episode_id=base.episode_id, version_id=base.id,
                          kind='finishing_edit', state='paused', idempotency_key='finishing:' + base.id,
                          payload={'evidence_revision': episode.evidence_revision})
                db.add(job)
            job.result = state.model_dump()
            job.stage = f'包装选择已保存：第 {state.step} 步'
            repo.audit(db, owner, 'finishing_saved', base.id, step=state.step, revision=state.revision)
            return state

    @app.post('/api/v1/versions/{version_id}/finishing/commit', response_model=VersionOut)
    def commit_finishing(version_id: str, body: CommitFinishing, auth=Depends(session), repo=Depends(repository)):
        owner = auth['user_id']
        with repo.transaction() as db:
            base, _, job, state, _ = load(db, repo, version_id, owner)
            if body.revision != state.revision:
                raise DomainError('FINISHING_CONFLICT', '选择已更新，请重新读取再生成。', 409)
            if state.completed != 3 or job is None:
                raise DomainError('FINISHING_INCOMPLETE', '先依次保存文字、音效、旁白与包装。', 409)
            if state.generated_version_id:
                return version_out(repo.owned(db, Version, state.generated_version_id, owner))
            # This workflow cannot change source spans. The exact approved narration
            # can be reused with new text styling/sounds/frames, without another paid call.
            version = create_version_record(db, repo, base.episode_id, state.draft, owner, reuse_finishing_voice=True)
            state.generated_version_id = version.id
            job.result = state.model_dump()
            job.stage = '已另存包装版本，待合成与预览'
            repo.audit(db, owner, 'finishing_committed', version.id, base_version_id=base.id)
            return version_out(version)
