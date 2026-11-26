"""Real, bounded director conversation over the existing DeepSeek transport.

The caller persists messages, permissions, jobs and the budget reservation. This
adapter proposes at most one typed action; it cannot execute any action. No
provider fallback or automatic retry is introduced.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .deepseek import DeepSeekDirector, empty_usage
from .director import DirectorError, _invalid_constant, _unique_object
from .director_reply_mode import discussion_only, reply_requirements


ACTION_KINDS = frozenset(('revise_directions', 'revise_narration', 'revise_subtitles', 'revise_cut', 'review_sequence', 'navigate'))


class _Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class _Proposal(_Strict):
    title: str = Field(min_length=1, max_length=60)
    explanation: str = Field(min_length=1, max_length=300)

    @field_validator('title', 'explanation')
    @classmethod
    def nonblank(cls, text: str):
        if not text.strip():
            raise ValueError('empty proposal text')
        return text.strip()


class ReviseDirections(_Proposal):
    kind: Literal['revise_directions']
    requirement: str = Field(min_length=1, max_length=1000)

    @field_validator('requirement')
    @classmethod
    def nonblank_requirement(cls, text: str):
        if not text.strip():
            raise ValueError('empty requirement')
        return text.strip()


class ReviseNarration(_Proposal):
    kind: Literal['revise_narration']
    narration_text: str = Field(min_length=1, max_length=120)

    @field_validator('narration_text')
    @classmethod
    def nonblank_narration(cls, text: str):
        if not text.strip():
            raise ValueError('empty narration')
        return text.strip()


class SubtitleCorrection(_Strict):
    finding: int = Field(ge=1, le=3)
    text: str = Field(min_length=1, max_length=160)

    @field_validator('text')
    @classmethod
    def valid_text(cls, text):
        if not text.strip() or any(ord(c) < 32 for c in text):
            raise ValueError('invalid subtitle')
        return text.strip()


class ReviseSubtitles(_Proposal):
    kind: Literal['revise_subtitles']
    corrections: list[SubtitleCorrection] = Field(min_length=1, max_length=3)

    @field_validator('corrections')
    @classmethod
    def unique_findings(cls, values):
        if len({v.finding for v in values}) != len(values):
            raise ValueError('duplicate subtitle correction')
        return values


class CutSelection(_Strict):
    shot: int = Field(ge=1, le=8)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=100)

    @field_validator('reason')
    @classmethod
    def readable(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError('invalid cut reason')
        return value.strip()


class EventDecision(_Strict):
    event: str = Field(pattern=r'^e[1-9][0-9]?$', max_length=3)
    keep: bool
    role: Literal['setup', 'action', 'result', 'aside', 'repetition', 'uncertain']
    reason: str = Field(min_length=1, max_length=100)
    risk: str = Field(max_length=100)

    @field_validator('reason')
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError('empty event reason')
        return value.strip()


class ReviseCut(_Proposal):
    kind: Literal['revise_cut']
    selections: list[CutSelection] | None = Field(default=None, min_length=1, max_length=8)
    event_decisions: list[EventDecision] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode='after')
    def one_plan(self):
        if (self.selections is None) == (self.event_decisions is None):
            raise ValueError('one cut plan required')
        return self


class ReviewSequence(_Proposal):
    kind: Literal['review_sequence']
    start: float = Field(ge=0, le=90, allow_inf_nan=False)
    end: float = Field(gt=0, le=90, allow_inf_nan=False)

    @model_validator(mode='after')
    def window(self):
        if not self.start < self.end or self.end - self.start > 24:
            raise ValueError('invalid review window')
        return self


class Navigate(_Proposal):
    kind: Literal['navigate']
    target_step: Literal['materials', 'direction', 'edit', 'preview']


ChatAction = Annotated[ReviseDirections | ReviseNarration | ReviseSubtitles | ReviseCut | ReviewSequence | Navigate, Field(discriminator='kind')]


class ChatReply(_Strict):
    reply: str = Field(min_length=1, max_length=2000)
    action: ChatAction | None = None

    @field_validator('reply')
    @classmethod
    def nonblank_reply(cls, text: str):
        if not text.strip():
            raise ValueError('empty reply')
        return text.strip()


def parse_chat_reply(content: str, *, available_actions=()) -> ChatReply:
    """Strict JSON validation is not a factual-quality endorsement."""
    if not isinstance(content, str) or len(content.encode('utf-8')) > 16000:
        raise DirectorError('INVALID_MODEL_OUTPUT', '导演回复格式或长度无效。')
    text = content.strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```', text, re.IGNORECASE)
        if not match:
            raise DirectorError('INVALID_MODEL_OUTPUT', '导演回复的 JSON 包装无效。')
        text = match[1]
    try:
        raw = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        reply = ChatReply.model_validate(raw)
    except ValidationError as exc:
        # Model values and arbitrary extra-field names may contain private text.
        # Keep only schema-owned locations and error types, never input/context.
        models = (ChatReply, _Proposal, ReviseDirections, ReviseNarration,
                  ReviseSubtitles, SubtitleCorrection, ReviseCut, CutSelection, EventDecision,
                  ReviewSequence, Navigate)
        fields = set().union(*(model.model_fields for model in models)) | ACTION_KINDS
        first = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        location = '.'.join(str(part) if type(part) is int else part if part in fields else '?'
                            for part in first['loc'])
        kind = first['type'] if re.fullmatch(r'[a-z_]{1,60}', first['type']) else 'invalid'
        raise DirectorError('INVALID_MODEL_OUTPUT',
                            f'导演回复结构校验失败（{location or "result"}：{kind}）。') from None
    except (ValueError, TypeError, RecursionError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '导演回复不是可读取的单个JSON对象。') from None
    if reply.action is not None and reply.action.kind not in available_actions:
        raise DirectorError('INVALID_MODEL_OUTPUT', '导演提出了当前不可用的动作（action.kind）。')
    return reply


def _json_value(value, depth=0):
    if depth > 16:
        raise ValueError('context too deeply nested')
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('context keys must be strings')
        for item in value.values():
            _json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_value(item, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError('context contains a non-json value')


def _compact_identifiers(context):
    """Short references on the wire only; source text and persisted IDs stay exact."""
    keys = {'id', 'asset_id', 'evidence_id', 'version_id', 'cue_id', 'focus_cue_id'}
    lists = {'asset_ids', 'evidence_ids', 'version_ids', 'cue_ids', 'speech_evidence_ids', 'unlinked_evidence_ids', 'protected_speech_evidence_ids'}
    found = []
    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and isinstance(item, str) and item not in found:
                    found.append(item)
                if key in lists and isinstance(item, list):
                    for reference in item:
                        if isinstance(reference, str) and reference not in found:
                            found.append(reference)
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect(context)
    aliases = {}
    serial = 1
    for value in found:
        if not re.fullmatch(r'[a-f0-9]{32}', value):
            continue
        while f'r{serial}' in found:
            serial += 1
        aliases[value] = f'r{serial}'
        serial += 1
    def replace(value):
        if isinstance(value, dict):
            return {key: aliases.get(item, item) if key in keys and isinstance(item, str)
                    else [aliases.get(ref, ref) if isinstance(ref, str) else replace(ref) for ref in item]
                    if key in lists and isinstance(item, list)
                    else replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return value
    return replace(context)


def prepare_chat_input(context: dict) -> tuple[str, list[str]]:
    try:
        if not isinstance(context, dict):
            raise ValueError('context must be an object')
        _json_value(context)
        available = context.get('available_actions', [])
        if (not isinstance(available, list) or any(type(kind) is not str or kind not in ACTION_KINDS for kind in available)
                or len(available) != len(set(available))):
            raise ValueError('invalid action capability list')
        from .director_cut import requested_speech_ids
        protected = sorted(requested_speech_ids(context))
        enriched = {**context, 'protected_speech_evidence_ids': protected} if protected else context
        from .story_events import build
        events = build(context)
        if events:
            enriched = {**enriched, 'story_events': events}
        enriched = {k: v for k, v in enriched.items() if k != 'story_event_catalog'}
        from .story_events import requested, packet
        if requested(context):
            enriched = packet(context)
            available = ['revise_cut']
        elif discussion_only(context):
            # Keep evidence/history verbatim, but do not advertise proposal
            # machinery for an explicitly conversational turn.
            available = []
            enriched = {k: v for k, v in enriched.items() if k != 'story_events'}
            enriched['available_actions'] = []
        # Put the actual current request after supporting material. This only
        # changes wire order, never the saved conversation or source evidence.
        if 'question' in enriched:
            enriched = {k: v for k, v in enriched.items() if k != 'question'} | {'question': enriched['question']}
        text = json.dumps({'untrusted_context': _compact_identifiers(enriched)}, ensure_ascii=False, allow_nan=False,
                          separators=(',', ':'))
        return text, available
    except (ValueError, TypeError, RecursionError):
        raise DirectorError('INVALID_CHAT_CONTEXT', '导演对话上下文格式无效，尚未发送请求。', usage=empty_usage()) from None


def load_chat_prompt(context=None):
    try:
        from .story_events import requested
        root = Path(__file__).with_name('prompts')
        if requested(context):
            return root.joinpath('director_events.txt').read_text(encoding='utf-8')
        prompt = root.joinpath('director_chat.txt').read_text(encoding='utf-8')
        actions = ('本轮仅讨论，action必须为null；可以解释取舍，但不能声称已经执行或提供待点击的修改卡。'
                   if discussion_only(context) else root.joinpath('director_chat_actions.txt').read_text(encoding='utf-8'))
        return prompt.replace('{{ACTION_RULES}}', actions).replace('{{REPLY_REQUIREMENTS}}', reply_requirements(context))
    except OSError:
        raise DirectorError('CHAT_PROMPT_UNAVAILABLE', '导演对话准备失败，尚未发送请求。', usage=empty_usage()) from None


def input_fits(context):
    user, _ = prepare_chat_input(context)
    return len((load_chat_prompt(context) + user).encode('utf-8')) + 2048 <= DeepSeekDirectorChat.input_limit


class DeepSeekDirectorChat(DeepSeekDirector):
    # Covers the existing audited peak tariff: 32K * 2 + 2048 * 8 / 1M.
    # This is a reservation ceiling, not the price charged for every message.
    required_reservation_yuan = .09
    input_limit = 32000
    max_output_tokens = 2048

    def _guard(self):
        if self._config.get('DIRECTOR_PROVIDER') != 'deepseek':
            raise DirectorError('PROVIDER_NOT_ALLOWED', '导演对话需使用当前已适配的 DeepSeek 编导配置，未自动切换供应商。')
        super()._guard()

    def ready(self):
        try:
            self._guard()
            return True, '导演对话已就绪；发送消息后调用模型，修改建议仍需你确认。'
        except DirectorError as exc:
            return False, exc.message

    async def chat(self, context: dict, *, reservation=None):
        self._guard()
        user, available_actions = prepare_chat_input(context)
        prompt = load_chat_prompt(context)
        if len((prompt + user).encode('utf-8')) + 2048 > self.input_limit:
            raise DirectorError('INPUT_LIMIT', '本次对话上下文超出费用预留范围，尚未发送请求。', usage=empty_usage())
        content, usage = await self.request([
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': user},
        ], reservation)
        try:
            return {'result': parse_chat_reply(content, available_actions=available_actions), 'usage': usage}
        except DirectorError as exc:
            exc.usage = usage
            raise
