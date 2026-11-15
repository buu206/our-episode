"""Shared, deterministic edit contract. Transitions belong to the incoming shot."""
from dataclasses import dataclass
import math
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, model_serializer
from .editorial_plan import EditorialBeat, compile_editorial, validate_editorial_sources


class VisualTarget(BaseModel):
    """Explicit source-frame rectangle, normalized; never an inferred identity."""
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=.08, le=1)
    height: float = Field(ge=.08, le=1)

    @model_validator(mode='after')
    def inside_frame(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError('视觉目标超出原画面。')
        return self


class ContourPoint(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class VisualBeat(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    kind: Literal['focus', 'burst', 'comic_space', 'cutout', 'headline']
    source_time: float = Field(ge=0)
    duration: float = Field(ge=.4, le=2.5)
    target: VisualTarget
    text: str = Field(default='', max_length=8)
    position: Literal['top_left', 'top_right', 'bottom_left', 'bottom_right'] = 'bottom_right'
    contour: list[ContourPoint] | None = Field(default=None, min_length=3, max_length=64)

    @model_serializer(mode='wrap')
    def compatible_wire(self, handler):
        data = handler(self)
        if self.contour is None:
            data.pop('contour', None)
        return data

    @model_validator(mode='after')
    def readable(self):
        if any(ord(c) < 32 for c in self.text) or self.kind in ('burst', 'headline') and not self.text.strip():
            raise ValueError('表情花字需提供简短可显示文字。')
        if (self.kind == 'cutout') != (self.contour is not None):
            raise ValueError('抠图贴纸必须提供经审阅的轮廓，其他效果不接受轮廓。')
        if self.contour:
            points = self.contour
            r = self.target
            if any(not (r.x <= p.x <= r.x+r.width+1e-6 and r.y <= p.y <= r.y+r.height+1e-6) for p in points):
                raise ValueError('抠图轮廓超出来源目标。')
            area = abs(sum(a.x*b.y-b.x*a.y for a,b in zip(points,points[1:]+points[:1]))) / 2
            if area < .0001:
                raise ValueError('抠图轮廓面积无效。')
            def cross(a,b,c):
                return (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x)
            for i,(a,b) in enumerate(zip(points,points[1:]+points[:1])):
                for j,(c,d) in enumerate(zip(points,points[1:]+points[:1])):
                    if j <= i+1 or i == 0 and j == len(points)-1:
                        continue
                    if cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0:
                        # Collinear, disjoint edges are legal.
                        if max(min(a.x,b.x),min(c.x,d.x)) <= min(max(a.x,b.x),max(c.x,d.x)) and max(min(a.y,b.y),min(c.y,d.y)) <= min(max(a.y,b.y),max(c.y,d.y)):
                            raise ValueError('抠图轮廓不能自交。')
        return self


class SubtitleEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    cue_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    text: str = Field(max_length=500)
    hidden: bool = False
    reviewed: bool = True

    @model_validator(mode='after')
    def displayable(self):
        if not self.hidden and not self.text.strip():
            raise ValueError('空字幕请选不显示此句。')
        if any(ord(c)<32 and c!='\n' for c in self.text) or self.text.count('\n')>1:
            raise ValueError('字幕最多手动分成两行。')
        return self


class EditSegment(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    minimum_duration: ClassVar[float] = .1
    asset_id: str = Field(min_length=1, max_length=128)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    caption: str = Field(max_length=100)
    purpose: str = Field(default='', max_length=200)
    evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    transition: Literal['cut', 'dissolve', 'fade_black'] = 'cut'
    transition_duration: float = Field(default=0, ge=0, le=.5, allow_inf_nan=False)
    visual_beats: list[VisualBeat] = Field(default_factory=list, max_length=4)
    editorial: EditorialBeat | None = None
    subtitle_edits: list[SubtitleEdit] = Field(default_factory=list, max_length=150)

    @model_serializer(mode='wrap')
    def compatible_editorial_wire(self, handler):
        data = handler(self)
        if self.editorial is None:
            data.pop('editorial', None)
        if not self.subtitle_edits:
            data.pop('subtitle_edits', None)
        return data

    @model_validator(mode='after')
    def valid_shot(self):
        if len({s.cue_id for s in self.subtitle_edits}) != len(self.subtitle_edits):
            raise ValueError('同一句字幕不能重复修改。')
        if (not self.asset_id.strip() or self.end <= self.start
                or self.end - self.start < self.minimum_duration - 1e-9):
            raise ValueError('invalid segment')
        if (len(set(self.evidence_ids)) != len(self.evidence_ids)
                or any(not value.strip() or len(value) > 128 for value in self.evidence_ids)):
            raise ValueError('invalid evidence references')
        if self.transition == 'cut' and self.transition_duration != 0:
            raise ValueError('a cut has no overlap')
        if self.transition != 'cut' and self.transition_duration < .1:
            raise ValueError('transition must last 0.1–0.5 seconds')
        if any(b.source_time < self.start or b.source_time + b.duration > self.end + 1e-6 for b in self.visual_beats):
            raise ValueError('视觉强调不能超出当前片段。')
        if self.editorial:
            if self.visual_beats or self.caption.strip():
                raise ValueError('编导安排使用独立版式，请清空旧花字和空间特效。')
            beat = self.editorial
            if beat.treatment != 'none' and (beat.source_time < self.start or beat.source_time+beat.duration>self.end+1e-6):
                raise ValueError('编导包装不能超出当前选段。')
        return self


def _rows(segments):
    return [item.model_dump() if isinstance(item, BaseModel) else item for item in segments]


def validate_transitions(segments) -> None:
    rows = _rows(segments)
    for index, item in enumerate(rows):
        kind = item.get('transition', 'cut')
        duration = item.get('transition_duration', 0)
        if (kind not in ('cut', 'dissolve', 'fade_black') or type(duration) not in (int, float)
                or not math.isfinite(duration)):
            raise ValueError('转场参数无效。')
        if kind == 'cut':
            if duration != 0:
                raise ValueError('直接切换不需要转场时长。')
            continue
        if index == 0 or not .1 <= duration <= .5:
            raise ValueError('首段不能添加转场，其余转场时长为 0.1–0.5 秒。')
        previous = rows[index - 1]
        overlap = round(duration * 30) / 30
        if max(duration, overlap) > min(item['end'] - item['start'], previous['end'] - previous['start']) / 2 + 1e-9:
            raise ValueError('转场不能超过相邻任一片段时长的一半。')
        if item.get('_preserve_expression') or previous.get('_preserve_expression'):
            raise ValueError('已确认的采访表达应完整保留，请使用直接切换。')


@dataclass(frozen=True)
class ClipTiming:
    start: float
    end: float
    duration: float
    overlap: float


def timeline(segments) -> list[ClipTiming]:
    """Frame-aligned for transitions; preserve legacy all-cut timing."""
    rows = _rows(segments)
    validate_transitions(rows)
    quantized = any(item.get('transition', 'cut') != 'cut' for item in rows)
    result, cursor = [], 0.0
    for item in rows:
        duration = item['end'] - item['start']
        if quantized:
            duration = math.ceil(duration * 30 - 1e-8) / 30
        overlap = round(item.get('transition_duration', 0) * 30) / 30
        start = cursor - overlap
        cursor = start + duration
        result.append(ClipTiming(start, cursor, duration, overlap))
    return result


def validate_speech_boundaries(segments, evidence) -> None:
    """Never trim a confirmed utterance or hide its edges under a crossfade."""
    rows = _rows(segments)
    times = timeline(rows)
    speech = [item for item in evidence if item.get('kind') == 'speech']
    tolerance = .03
    for index, segment in enumerate(rows):
        incoming = times[index].overlap
        outgoing = times[index + 1].overlap if index + 1 < len(rows) else 0
        for line in speech:
            if line['asset_id'] != segment['asset_id']:
                continue
            if min(line['end'], segment['end']) - max(line['start'], segment['start']) <= tolerance:
                continue
            if segment['start'] > line['start'] + tolerance or segment['end'] < line['end'] - tolerance:
                raise ValueError('选段截断了已确认的原声话轮，请保留完整表达。')
            if (incoming and line['start'] < segment['start'] + incoming - tolerance
                    or outgoing and line['end'] > segment['end'] - outgoing + tolerance):
                raise ValueError('转场会盖住已确认的原声，请改为直接切换或选择有留白的选段。')


def validate_director_segments(segments, evidence) -> None:
    """A new director contract stays grounded after manual edits and restarts."""
    from .source_windows import planning_sources
    sources = {row['id']: row for row in planning_sources(evidence) if row.get('id')}
    for item in _rows(segments):
        shot = EditSegment.model_validate(item)
        # Current model input has no spatial observations. Do not let a model
        # invent a face/bottle location just because the renderer supports it.
        if shot.visual_beats:
            raise ValueError('导演尚无空间定位依据，请先通过审阅指定视觉目标。')
        if not shot.purpose.strip() or not shot.evidence_ids:
            raise ValueError('导演选段需保留镜头用途和来源依据。')
        refs = [sources.get(key) for key in shot.evidence_ids]
        if any(row is None or row['asset_id'] != shot.asset_id
               or min(row['end'], shot.end) <= max(row['start'], shot.start) for row in refs):
            raise ValueError('镜头引用的依据与当前素材区间不匹配。')
        if not any(row['start'] <= shot.start and row['end'] >= shot.end for row in refs):
            raise ValueError('镜头区间不能超出来源依据。')
        if any(row.get('kind') == 'continuity' for row in refs) and shot.caption.strip():
            raise ValueError('连续时间容器不支持整段花字，请把文案绑定到具体话轮或画面依据。')
        validate_editorial_sources(shot, sources)
    compile_editorial(_rows(segments))
    validate_speech_boundaries(segments, evidence)
