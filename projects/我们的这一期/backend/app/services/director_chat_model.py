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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .deepseek import DeepSeekDirector, empty_usage
from .director import DirectorError, _invalid_constant, _unique_object


ACTION_KINDS = frozenset(('revise_directions', 'revise_narration', 'navigate'))


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


class Navigate(_Proposal):
    kind: Literal['navigate']
    target_step: Literal['materials', 'direction', 'edit', 'preview']


ChatAction = Annotated[ReviseDirections | ReviseNarration | Navigate, Field(discriminator='kind')]


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
        if reply.action is not None and reply.action.kind not in available_actions:
            raise ValueError('action unavailable in this context')
        return reply
    except (ValueError, TypeError, RecursionError, ValidationError):
        raise DirectorError('INVALID_MODEL_OUTPUT', '导演回复未通过结构或可用动作校验。') from None


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


def prepare_chat_input(context: dict) -> tuple[str, list[str]]:
    try:
        if not isinstance(context, dict):
            raise ValueError('context must be an object')
        _json_value(context)
        available = context.get('available_actions', [])
        if (not isinstance(available, list) or any(type(kind) is not str or kind not in ACTION_KINDS for kind in available)
                or len(available) != len(set(available))):
            raise ValueError('invalid action capability list')
        text = json.dumps({'untrusted_context': context}, ensure_ascii=False, allow_nan=False)
        return text, available
    except (ValueError, TypeError, RecursionError):
        raise DirectorError('INVALID_CHAT_CONTEXT', '导演对话上下文格式无效，尚未发送请求。', usage=empty_usage()) from None


def load_chat_prompt():
    try:
        return Path(__file__).with_name('prompts').joinpath('director_chat.txt').read_text(encoding='utf-8')
    except OSError:
        raise DirectorError('CHAT_PROMPT_UNAVAILABLE', '导演对话准备失败，尚未发送请求。', usage=empty_usage()) from None


def input_fits(context):
    user, _ = prepare_chat_input(context)
    return len((load_chat_prompt() + user).encode('utf-8')) + 2048 <= DeepSeekDirectorChat.input_limit


class DeepSeekDirectorChat(DeepSeekDirector):
    # Covers the existing audited peak tariff: 24K * 2 + 2048 * 8 / 1M.
    # This is a reservation ceiling, not the price charged for every message.
    required_reservation_yuan = .07
    input_limit = 24000
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
        prompt = load_chat_prompt()
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
