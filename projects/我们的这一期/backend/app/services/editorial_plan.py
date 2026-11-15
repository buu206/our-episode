"""Source-linked post-production intentions, separate from spatial effects.

The language model selects a treatment, not a face coordinate or executable filter.
Compilation is deterministic; a valid plan is not proof of editorial quality.
"""
from __future__ import annotations

from typing import Literal
import math
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError


class EditorialBeat(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    role: Literal['setup', 'action', 'reaction', 'closing']
    treatment: Literal['none', 'chapter', 'accent']
    reason: str = Field(min_length=1, max_length=160)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    source_time: float | None = Field(default=None, ge=0)
    duration: float = Field(default=0, ge=0, le=6)
    text: str = Field(default='', max_length=10)
    detail: str = Field(default='', max_length=40)

    @model_validator(mode='after')
    def executable(self):
        if (not self.reason.strip() or any(not x.strip() or len(x)>128 for x in self.evidence_ids)
                or len(set(self.evidence_ids)) != len(self.evidence_ids)
                or any(ord(c)<32 for c in self.text+self.detail)):
            raise PydanticCustomError('editorial_invalid_text', '编导安排需要有效依据、简短理由和可显示文字。')
        if self.treatment == 'none':
            if self.source_time is not None or self.duration != 0 or self.text or self.detail:
                raise PydanticCustomError('editorial_none_has_effect', '留白不能携带隐藏的特效或文案。')
        else:
            if self.source_time is None or not self.text.strip():
                raise PydanticCustomError('editorial_missing_timing', '编导包装需要明确时点和文案。')
            if self.treatment == 'chapter':
                if self.role not in ('setup','closing') or not self.detail.strip():
                    raise PydanticCustomError('editorial_chapter_role', '整屏版式仅用于有说明文字的交代或收尾节点。')
                if self.duration < max(2, len(self.detail)/8+1):
                    raise PydanticCustomError('editorial_reading_time', '整屏说明停留过短，请缩短文字或增加阅读时间。')
            elif not .6 <= self.duration <= 2.5 or self.detail:
                raise PydanticCustomError('editorial_accent_duration', '局部花字仅接受0.6–2.5秒的短标题。')
        return self


def compile_editorial(segments, offset=0):
    """Immutable plan, retaining explicit no-effect decisions and source times."""
    from .editing import timeline
    if not math.isfinite(offset) or offset < 0:
        raise ValueError('invalid editorial offset')
    times=timeline(segments)
    result=[]
    for index,(shot,timing) in enumerate(zip(segments,times)):
        raw=shot.get('editorial')
        if raw is None:
            continue
        beat=EditorialBeat.model_validate(raw)
        if shot.get('visual_beats') or shot.get('caption','').strip():
            raise ValueError('编导版式不能与旧花字或手工空间特效叠加，请分版比较。')
        if beat.treatment == 'none':
            result.append({'segment':index,'start':offset+timing.start,'end':offset+timing.end,
                           'beat':beat})
            continue
        if shot.get('_preserve_expression'):
            raise ValueError('已确认采访表达不添加编导包装。')
        outgoing=times[index+1].overlap if index+1<len(times) else 0
        at=beat.source_time-shot['start']
        if at < timing.overlap-1e-6 or at+beat.duration > min(
                shot['end']-shot['start'],timing.duration-outgoing)+1e-6:
            raise ValueError('编导包装不能越过片段边界或叠加片间转场。')
        result.append({'segment':index,'start':offset+timing.start+at,
                       'end':offset+timing.start+at+beat.duration,'beat':beat})
    active=[x for x in result if x['beat'].treatment!='none']
    if len(active)>8 or sum(x['beat'].treatment=='chapter' for x in active)>2:
        raise ValueError('最多两处整屏说明、八处包装，请为过程留白。')
    if times and times[-1].end >= 10 and sum(x['beat'].duration for x in active)>times[-1].end*.6:
        raise ValueError('包装占比过高，请为原画面留出时间。')
    return result


def validate_editorial_sources(shot, sources):
    beat=shot.editorial
    if beat is None:
        return
    if not set(beat.evidence_ids).issubset(set(shot.evidence_ids)):
        raise ValueError('编导节点必须引用当前选段已有的依据。')
    refs=[sources.get(key) for key in beat.evidence_ids]
    if any(row is None or row['asset_id']!=shot.asset_id for row in refs):
        raise ValueError('编导节点引用的来源不存在或不属于当前素材。')
    if beat.treatment!='none' and not any(row.get('kind')!='continuity' and row['start']<=beat.source_time and
            row['end']>=beat.source_time+beat.duration for row in refs):
        raise ValueError('编导包装时段超出所引用的依据。')
