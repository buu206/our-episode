"""Guarded text proposals from user-approved source notes; no media analysis here.

The caller owns persisted authorisation, evidence confirmation and budget records.
It must reserve ``required_reservation_yuan`` before supplying a reservation whose
validity is checked by ``budget_check``. A failed/uncertain billed request must not
release that reservation. This module never enables cloud processing itself.
"""

from __future__ import annotations

import inspect
import json
import math
import re
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .editing import EditSegment, validate_director_segments, validate_transitions
from .source_windows import source_windows


class DirectorError(Exception):
    """Public, sanitised error; provider responses and credentials are never kept."""

    def __init__(self, code: str, message: str, *, usage: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.usage = usage


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Span(_StrictModel):
    asset_id: str = Field(min_length=1, max_length=128)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note", "asset_id")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def ordered(self) -> Span:
        if self.end <= self.start:
            raise ValueError("end must follow start")
        return self


class InputSpan(Span):
    # Old, untyped comparison fixtures remain readable. New production input
    # carries both fields so a static observation cannot impersonate speech.
    id: str | None = Field(default=None, min_length=1, max_length=80)
    kind: Literal["visual", "speech", "continuity"] | None = None

    @model_validator(mode="after")
    def identifiable(self) -> InputSpan:
        if (self.id is None) != (self.kind is None) or self.id is not None and not self.id.strip():
            raise ValueError("typed evidence needs both a nonblank id and kind")
        return self


class EditPlan(_StrictModel):
    segments: list[EditSegment] = Field(min_length=1, max_length=30)
    narration_text: str = Field(max_length=120)

    @model_validator(mode="after")
    def bounded_duration(self) -> EditPlan:
        validate_transitions(self.segments)
        from .editorial_plan import compile_editorial
        compile_editorial([s.model_dump() for s in self.segments])
        # Existing renderer reserves up to 30 seconds for a separate voice intro.
        limit = 59.9 if self.narration_text.strip() else 90
        if sum(item.end - item.start for item in self.segments) > limit + 1e-9:
            raise ValueError("edit plan exceeds duration limit")
        return self


class Direction(_StrictModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)
    evidence: list[Span] = Field(min_length=1, max_length=40)
    # None is accepted only to read pre-existing directions/demo fixtures.
    edit_plan: EditPlan | None = None

    @field_validator("title", "summary")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class DirectionResult(_StrictModel):
    directions: list[Direction] = Field(max_length=3)
    uncertainties: list[str] = Field(max_length=30)

    @field_validator("uncertainties")
    @classmethod
    def reasons_are_meaningful(cls, values: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 2000 for item in values):
            raise ValueError("invalid uncertainty")
        return values

    @model_validator(mode="after")
    def sufficient_result(self) -> DirectionResult:
        if not self.directions and not self.uncertainties:
            raise ValueError("no directions requires an uncertainty reason")
        if len({item.id for item in self.directions}) != len(self.directions):
            raise ValueError("direction ids must be unique")
        return self


class PlannedDirection(Direction):
    edit_plan: EditPlan


class PlannedDirectionResult(DirectionResult):
    directions: list[PlannedDirection] = Field(max_length=3)


def _assets_valid(assets: dict[str, float]) -> None:
    if not isinstance(assets, dict) or not assets:
        raise DirectorError("INVALID_ASSETS", "需要有效素材及其时长。")
    for key, duration in assets.items():
        if (not isinstance(key, str) or not key.strip() or len(key) > 128
                or type(duration) not in (int, float)
                or not math.isfinite(duration) or duration <= 0):
            raise DirectorError("INVALID_ASSETS", "素材标识或时长无效。")


def _check_span(span: Span, assets: dict[str, float]) -> None:
    if span.asset_id not in assets or span.end > assets[span.asset_id]:
        raise DirectorError("INVALID_EVIDENCE", "提案引用了不存在的素材或超出素材范围。")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise ValueError("non-finite JSON number")


def parse_result(content: str, assets: dict[str, float], *, require_edit_plan: bool = False) -> DirectionResult:
    """Accept only a JSON object or a single complete outer JSON code fence.

    Schema and time bounds are deterministic checks, not proof of factual prose.
    Names, quotations and interpretation still need human confirmation.
    """
    _assets_valid(assets)
    if not isinstance(content, str) or len(content.encode("utf-8")) > 100_000:
        raise DirectorError("INVALID_MODEL_OUTPUT", "模型返回格式无效。")
    text = content.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text, re.IGNORECASE)
        if not match:
            raise DirectorError("INVALID_MODEL_OUTPUT", "模型返回的 JSON 包装无效。")
        text = match.group(1)
    try:
        data = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        result_type = PlannedDirectionResult if require_edit_plan else DirectionResult
        result = result_type.model_validate(data)
    except ValidationError as exc:
        # Report only allowlisted schema locations/types. Never include a
        # provider value, arbitrary extra-field name, prose, or full response.
        fields = set(DirectionResult.model_fields) | set(Direction.model_fields) | set(EditPlan.model_fields) | set(EditSegment.model_fields)
        from .editorial_plan import EditorialBeat
        fields |= set(EditorialBeat.model_fields)
        first = exc.errors(include_input=False, include_context=False, include_url=False)[0]
        location = '.'.join(str(x) if type(x) is int else x if x in fields else '?' for x in first['loc'])
        kind = first['type'] if re.fullmatch(r'[a-z_]{1,60}', first['type']) else 'invalid'
        raise DirectorError("INVALID_MODEL_OUTPUT", f"模型返回未通过结构校验（{location or 'result'}：{kind}）。") from None
    except (ValueError, TypeError, RecursionError):
        raise DirectorError("INVALID_MODEL_OUTPUT", "模型返回未通过结构校验。") from None
    for direction in result.directions:
        for span in direction.evidence:
            _check_span(span, assets)
        if require_edit_plan and direction.edit_plan is None:
            raise DirectorError("INVALID_MODEL_OUTPUT", "模型提案缺少剪辑方案。")
        if direction.edit_plan is not None:
            for segment in direction.edit_plan.segments:
                if segment.subtitle_edits:
                    raise DirectorError('INVALID_MODEL_OUTPUT','模型不能代替用户确认或修改字幕。')
                if not any(source.asset_id == segment.asset_id and source.start <= segment.start
                           and source.end >= segment.end for source in direction.evidence):
                    raise DirectorError("UNGROUNDED_EVIDENCE", "剪辑片段超出该方向引用的证据范围。")
    return result


def prepare_input(evidence: list[dict], assets: dict[str, float], background: str = "") -> str:
    """One input contract shared by live proposals and offline comparison packs."""
    _assets_valid(assets)
    if not isinstance(evidence, list) or not evidence:
        raise DirectorError("EVIDENCE_REQUIRED", "需要用户同意用于创作的素材依据。")
    try:
        if len(evidence) > 100 or not isinstance(background, str) or len(background) > 4000:
            raise ValueError("input limit")
        spans = _input_spans(evidence)
        for span in spans:
            _check_span(span, assets)
        payload = {"verified_evidence": [span.model_dump(exclude_none=True) for span in spans],
                   "background_context": background}
        windows = source_windows(evidence)
        if windows:
            payload['source_windows'] = windows
        from .editing_brief import build_editing_brief
        brief = build_editing_brief(spans, windows)
        if brief is not None:
            payload['editing_brief'] = brief
        text = json.dumps(payload, ensure_ascii=False)
        if len(text.encode("utf-8")) > 100_000:
            raise ValueError("input limit")
        return text
    except (ValueError, TypeError, ValidationError):
        raise DirectorError("INVALID_EVIDENCE", "素材依据格式或长度无效。") from None


def _input_spans(evidence: list[dict]) -> list[InputSpan]:
    spans = [InputSpan.model_validate(item) for item in evidence]
    if any(span.kind == "continuity" for span in spans):
        raise ValueError("source windows must be derived, never submitted as facts")
    ids = [span.id for span in spans if span.id is not None]
    if ids and (len(ids) != len(spans) or len(set(ids)) != len(ids)):
        raise ValueError("typed evidence needs unique ids for every item")
    return spans


def check_grounding(result: DirectionResult, evidence: list[dict]) -> None:
    try:
        spans = _input_spans(evidence)
    except (ValueError, TypeError, ValidationError):
        raise DirectorError("INVALID_EVIDENCE", "素材依据格式或标识无效。") from None
    spans += [InputSpan.model_validate(row) for row in source_windows(evidence)]
    by_id = {span.id: span for span in spans if span.id is not None}
    for direction in result.directions:
        for span in direction.evidence:
            if not any(source.asset_id == span.asset_id and source.start <= span.start
                       and source.end >= span.end for source in spans):
                raise DirectorError("UNGROUNDED_EVIDENCE", "提案引用了未获确认用于创作的素材区间。")
        if direction.edit_plan is None:
            continue  # Existing stored proposals without explicit edit plans.
        if not by_id and any(s.editorial for s in direction.edit_plan.segments):
            raise DirectorError('INVALID_EDIT_PLAN', '编导安排需要带标识的来源依据。')
        for segment in direction.edit_plan.segments:
            for evidence_id in segment.evidence_ids:
                source = by_id.get(evidence_id)
                if (source is None or not any(reference.asset_id == source.asset_id
                                   and min(reference.end, source.end) > max(reference.start, source.start)
                                   for reference in direction.evidence)):
                    raise DirectorError("UNGROUNDED_EVIDENCE", "剪辑片段引用了本方向以外的来源标识。")
        if by_id:
            try:
                # This same contract also protects subsequent manual edits and
                # rendering; all overlapping speech is checked, not just IDs
                # the model chose to disclose in its own references.
                validate_director_segments(direction.edit_plan.segments,
                                           evidence)
            except ValueError as exc:
                raise DirectorError("INVALID_EDIT_PLAN", str(exc)) from None


class CloudDirector:
    """Only the vetted Beijing endpoint and selected model are permitted.

    The three cloud switches must be actual True booleans. Merely supplying a key
    cannot unlock the service. ``budget_check(reservation)`` must return True and
    must validate a persisted reservation of at least 1.82 yuan. It may be async.
    The amount conservatively covers two requests at the candidate's highest
    published tier, 262144 input + 4096 output tokens each (rounded up to cents).
    Prices require renewed review before actual provider admission.
    """

    required_reservation_yuan = 1.82
    max_output_tokens = 4096
    _model = "qwen3-vl-plus"
    _base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def __init__(self, config: dict, *, client_factory: Callable | None = None,
                 budget_check: Callable | None = None):
        self._config = dict(config)
        self._client_factory = client_factory
        self._budget_check = budget_check

    def _guard(self) -> None:
        for key, code, message in (
            ("CLOUD_ENABLED", "CLOUD_DISABLED", "AI 分析未启用。"),
            ("CLOUD_RETENTION_VERIFIED", "RETENTION_UNVERIFIED", "供应商留存和删除期限尚未核实。"),
            ("CLOUD_REGION_VERIFIED", "REGION_UNVERIFIED", "境内处理范围尚未核实。"),
        ):
            if self._config.get(key) is not True:
                raise DirectorError(code, message)
        raw = self._config.get("MODEL_BASE_URL", "")
        if not isinstance(raw, str):
            raise DirectorError("ENDPOINT_NOT_ALLOWED", "模型服务地址不在已核实的境内白名单中。")
        try:
            parts = urlsplit(raw)
            allowed = (raw.rstrip("/") == self._base_url and parts.scheme == "https"
                       and parts.hostname == "dashscope.aliyuncs.com"
                       and parts.username is None and parts.password is None
                       and parts.port is None and not parts.query and not parts.fragment)
        except ValueError:
            allowed = False
        if not allowed:
            raise DirectorError("ENDPOINT_NOT_ALLOWED", "模型服务地址不在已核实的境内白名单中。")
        if self._config.get("MODEL_NAME") != self._model:
            raise DirectorError("MODEL_NOT_ALLOWED", "模型型号尚未获准使用。")
        key = self._config.get("MODEL_API_KEY")
        if not isinstance(key, str) or not key.strip() or any(c.isspace() for c in key):
            raise DirectorError("KEY_NOT_CONFIGURED", "尚未配置有效模型密钥。")

    def ready(self) -> tuple[bool, str]:
        try:
            self._guard()
            return True, "素材依据的文本提案接口就绪；每次调用仍需预算许可。"
        except DirectorError as exc:
            return False, exc.message

    async def _check_budget(self, reservation: Any, usage: dict) -> None:
        try:
            if self._budget_check is None or reservation is None:
                raise ValueError("missing permission")
            permitted = self._budget_check(reservation)
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if permitted is not True:
                raise ValueError("permission denied")
        except Exception:
            raise DirectorError("BUDGET_PERMISSION_REQUIRED", "调用前需有效的持久预算预留许可。", usage=usage) from None

    @classmethod
    def _usage(cls, raw: Any, request_id: Any, retries: int, uncertain_attempt: bool) -> dict:
        counts = raw if isinstance(raw, dict) else {}
        input_tokens, output_tokens = counts.get("prompt_tokens"), counts.get("completion_tokens")
        valid = (type(input_tokens) is int and 0 <= input_tokens <= 262144
                 and type(output_tokens) is int and 0 <= output_tokens <= cls.max_output_tokens)
        cost: float | None = None
        if valid and not uncertain_attempt:
            input_rate, output_rate = ((1, 10) if input_tokens <= 32000 else
                                       (1.5, 15) if input_tokens <= 128000 else (3, 30))
            cost = float((Decimal(input_tokens) * Decimal(str(input_rate))
                          + Decimal(output_tokens) * Decimal(output_rate)) / Decimal(1_000_000))
        return {
            "input_tokens": input_tokens if valid else None,
            "output_tokens": output_tokens if valid else None,
            "cost_yuan": cost,
            "cost_status": "known" if cost is not None else "unknown",
            "request_id": request_id if isinstance(request_id, str) and re.fullmatch(r"[\w.-]{1,128}", request_id, re.ASCII) else None,
            "model": cls._model,
            "retries": retries,
        }

    async def propose(self, evidence: list[dict], assets: dict[str, float],
                      background: str = "", *, reservation: Any = None) -> dict:
        self._guard()
        user_text = prepare_input(evidence, assets, background)
        unsent = {**self._usage(None, None, 0, False), "cost_yuan": 0.0, "cost_status": "not_sent"}
        await self._check_budget(reservation, unsent)
        try:
            prompt = Path(__file__).with_name("prompts").joinpath("director.txt").read_text(encoding="utf-8")
            client = (self._client_factory() if self._client_factory else
                      httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10), follow_redirects=False, trust_env=False))
        except Exception:
            raise DirectorError("CLIENT_INIT_FAILED", "模型客户端初始化失败，尚未发送请求。", usage=unsent) from None
        uncertain_attempt = False
        usage = unsent
        try:
            async with client:
                for attempt in range(2):
                    if attempt:
                        await self._check_budget(reservation, usage)
                    usage = self._usage(None, None, attempt, True)
                    try:
                        response = await client.post(
                            self._base_url + "/chat/completions",
                            headers={"Authorization": "Bearer " + self._config["MODEL_API_KEY"]},
                            json={"model": self._model, "temperature": 0.2,
                                  "max_tokens": self.max_output_tokens,
                                  "messages": [{"role": "system", "content": prompt},
                                               {"role": "user", "content": user_text}]},
                            follow_redirects=False,
                        )
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        uncertain_attempt = True
                        if attempt == 0:
                            continue
                        raise DirectorError("MODEL_CONNECTION_FAILED", "模型连接失败，预算保持占用待核对。", usage=usage) from None
                    except httpx.TimeoutException:
                        raise DirectorError("MODEL_TIMEOUT", "模型响应超时，费用未知，预算保持占用待核对。", usage=usage) from None
                    except httpx.RequestError:
                        raise DirectorError("MODEL_REQUEST_FAILED", "模型请求失败，预算保持占用待核对。", usage=usage) from None
                    if response.status_code != 200:
                        raise DirectorError("MODEL_HTTP_ERROR", "模型服务返回错误，预算保持占用待核对。", usage=usage)
                    try:
                        envelope = response.json()
                        if not isinstance(envelope, dict):
                            raise ValueError("invalid envelope")
                        usage = self._usage(envelope.get("usage"), envelope.get("id"), attempt, uncertain_attempt)
                        choice = envelope["choices"][0]
                        if choice.get("finish_reason") != "stop":
                            raise ValueError("incomplete response")
                        result = parse_result(choice["message"]["content"], assets, require_edit_plan=True)
                        check_grounding(result, evidence)
                    except DirectorError as exc:
                        exc.usage = usage
                        raise
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        raise DirectorError("INVALID_MODEL_OUTPUT", "模型返回未通过结构校验。", usage=usage) from None
                    return {"result": result, "usage": usage}
        except DirectorError:
            raise
        except Exception:
            raise DirectorError("CLIENT_FAILURE", "模型客户端执行失败，请核对预算状态。", usage=usage) from None
        raise DirectorError("MODEL_REQUEST_FAILED", "未取得有效模型结果。", usage=usage)
