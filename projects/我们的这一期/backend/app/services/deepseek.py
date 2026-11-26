"""DeepSeek Flash adapters. No automatic retries or provider fallback."""
import base64
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import time
from zoneinfo import ZoneInfo

import httpx

from .director import (CloudDirector, DirectorError, prepare_input, parse_result,
                       check_grounding, _unique_object, _invalid_constant)
from .provider_config import guard
from .vision import (PreparedVideo, FRAMES_PER_GROUP, VisionResult,
                     _validate_prepared, parse_observations)

MODEL = 'deepseek-flash'
MODELS = {MODEL, 'DeepSeek-V4.1-Flash-1017', 'deepseek-v4.1-flash-1017'}
BASE = 'https://api.deepseek.com'
# Price audit 2026-11-10; the audited remaining 2026 calendar has no holidays.
# https://www.beijing.gov.cn/zhengce/zhengcefagui/202511/t20251104_4258873.html
# A new year / earlier schedule requires renewed review, never a guessed tariff.
def price_band(timestamp):
    local = datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Shanghai'))
    if not '2026-11-10' <= local.date().isoformat() <= '2026-12-31':
        return None
    return 'peak' if local.weekday() < 5 and (9 <= local.hour < 12 or 14 <= local.hour < 18) else 'offpeak'


def empty_usage(status='not_sent', *, model=MODEL):
    return {'provider': 'deepseek', 'model': model, 'input_tokens': None, 'output_tokens': None,
            'request_id': None, 'retries': 0, 'cost_status': status,
            'cost_yuan': 0.0 if status == 'not_sent' else None}


def aggregate(records):
    if not records:
        return {**empty_usage(), 'requests': []}
    known = all(r['cost_status'] in ('known', 'not_sent') for r in records)
    return {**empty_usage('unknown'), 'requests': list(records),
            'input_tokens': sum(r.get('input_tokens') or 0 for r in records) if known else None,
            'output_tokens': sum(r.get('output_tokens') or 0 for r in records) if known else None,
            'cost_status': 'known' if known else 'unknown',
            'cost_yuan': float(sum(Decimal(str(r['cost_yuan'])) for r in records)) if known else None}


def response_usage(envelope, started, ended, input_limit, output_limit, *,
                   model=MODEL, models=MODELS, rates=('.04', '2', '8'), pricing_revision='2026-11-10'):
    usage = empty_usage('unknown', model=model)
    request_id = envelope.get('id')
    if isinstance(request_id, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', request_id):
        usage['request_id'] = request_id
    returned = envelope.get('model')
    if returned in models:
        usage['model_version'] = returned
    raw = envelope.get('usage', {})
    if not isinstance(raw, dict):
        return usage
    counts = [raw.get(k) for k in ('prompt_tokens', 'completion_tokens',
                                  'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')]
    if any(type(v) is not int or v < 0 for v in counts):
        return usage
    inputs, outputs, hit, miss = counts
    if inputs > input_limit or outputs > output_limit or hit + miss != inputs:
        return usage
    usage.update(input_tokens=inputs, output_tokens=outputs, cache_hit_tokens=hit, cache_miss_tokens=miss)
    created = envelope.get('created')
    band = price_band(started)
    if (not band or band != price_band(ended) or type(created) is not int
            or not started - 5 <= created <= ended + 5 or price_band(created) != band
            or returned not in models or not usage['request_id']):
        return usage
    divisor = 1 if band == 'peak' else 2
    cost = (Decimal(hit) * Decimal(rates[0]) + Decimal(miss) * Decimal(rates[1]) + Decimal(outputs) * Decimal(rates[2])) / (1_000_000 * divisor)
    usage.update(cost_yuan=float(cost), cost_status='known', price_band=band,
                 pricing_revision=pricing_revision, usage_basis='provider_tokens')
    return usage


class DeepSeekDirector(CloudDirector):
    transport_model = MODEL
    response_models = MODELS
    token_rates = ('.04', '2', '8')
    pricing_revision = '2026-11-10'

    required_reservation_yuan = .28  # 120K input*2 + 4096 output*8, rounded up
    input_limit = 120000
    max_output_tokens = 4096

    def _guard(self):
        guard(self._config, 'DEEPSEEK')
        if self._config.get('DEEPSEEK_BASE_URL', BASE) != BASE:
            raise DirectorError('ENDPOINT_NOT_ALLOWED', 'DeepSeek地址不在白名单中。')
        if self._config.get('DEEPSEEK_MODEL', MODEL) != MODEL:
            raise DirectorError('MODEL_NOT_ALLOWED', 'DeepSeek型号不在本轮验证范围内。')

    async def request(self, messages, reservation, input_limit=None):
        self._guard()
        usage = empty_usage(model=self.transport_model)
        # Avoid starting across known price boundaries. Max duration is 100s.
        now = time.time()
        if price_band(now) is None or price_band(now) != price_band(now + 110):
            raise DirectorError('PRICING_REVIEW_REQUIRED', '当前计费时段边界或价格日历需核对；尚未发送请求。', usage=usage)
        await self._check_budget(reservation, usage)
        try:
            client = self._client_factory() if self._client_factory else httpx.AsyncClient(
                timeout=httpx.Timeout(90, connect=10), trust_env=False, follow_redirects=False)
        except Exception:
            raise DirectorError('CLIENT_INIT_FAILED', '模型客户端初始化失败，尚未发送请求。', usage=usage) from None
        import asyncio
        try:
            async with client:
                await self._check_budget(reservation, usage)
                started = time.time()
                usage = empty_usage('unknown', model=self.transport_model)
                async with asyncio.timeout(100):
                    async with client.stream('POST', BASE + '/chat/completions', headers={
                        'Authorization': 'Bearer ' + self._config['DEEPSEEK_API_KEY']}, json={
                        'model': self.transport_model, 'thinking': {'type': 'disabled'}, 'temperature': .2,
                        'response_format': {'type': 'json_object'}, 'max_tokens': self.max_output_tokens,
                        'messages': messages}, follow_redirects=False) as response:
                        if response.status_code != 200:
                            raise DirectorError('MODEL_HTTP_ERROR', '模型请求失败，费用待核对。', usage=usage)
                        data = bytearray()
                        async for block in response.aiter_bytes():
                            data.extend(block)
                            if len(data) > 200_000:
                                raise DirectorError('INVALID_MODEL_OUTPUT', '模型响应超过本轮限制。', usage=usage)
                envelope = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                if not isinstance(envelope, dict):
                    raise ValueError('envelope')
                usage = response_usage(envelope, started, time.time(), input_limit or self.input_limit, self.max_output_tokens,
                    model=self.transport_model, models=self.response_models, rates=self.token_rates,
                    pricing_revision=self.pricing_revision)
                if envelope.get('model') not in self.response_models:
                    raise DirectorError('MODEL_NOT_ALLOWED', '服务返回的型号不在已核实范围内。', usage=usage)
                choice = envelope['choices'][0]
                if choice.get('finish_reason') != 'stop' or not isinstance(choice['message']['content'], str):
                    raise ValueError('incomplete')
                return choice['message']['content'], usage
        except DirectorError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise DirectorError('MODEL_TIMEOUT', '模型响应超时，费用未知；不会自动重提。', usage=usage) from None
        except Exception:
            raise DirectorError('MODEL_REQUEST_FAILED', '模型处理失败，请核对费用后再操作。', usage=usage) from None

    def _proposal_input(self, evidence, assets, background):
        return prepare_input(evidence, assets, background)

    def _proposal_prompt(self):
        return Path(__file__).with_name('prompts').joinpath('director.txt').read_text()

    async def propose(self, evidence, assets, background='', *, reservation=None):
        self._guard()
        user = self._proposal_input(evidence, assets, background)
        prompt = self._proposal_prompt()
        if len((user + prompt).encode()) + 2048 > self.input_limit:
            raise DirectorError('INPUT_LIMIT', '编导输入超出本轮费用预留范围。', usage=empty_usage())
        content, usage = await self.request([{'role': 'system', 'content': prompt},
                                             {'role': 'user', 'content': user}], reservation)
        try:
            result = parse_result(content, assets, require_edit_plan=True)
            check_grounding(result, evidence)
            return {'result': result, 'usage': usage}
        except DirectorError as exc:
            exc.usage = usage
            raise


class DeepSeekVision(DeepSeekDirector):
    max_output_tokens = 2048
    input_limit = 40000

    @staticmethod
    def estimate(prepared):
        _validate_prepared(prepared)
        return .10 * ((len(prepared.frames) + FRAMES_PER_GROUP - 1) // FRAMES_PER_GROUP)

    async def analyze(self, prepared: PreparedVideo, *, reservation=None):
        self._guard()
        amount = self.estimate(prepared)
        prompt = Path(__file__).with_name('prompts').joinpath('vision.txt').read_text()
        records, observations, uncertainties = [], [], []
        try:
            for offset in range(0, len(prepared.frames), FRAMES_PER_GROUP):
                self._guard()
                # Bind each group to the persisted full-packet reservation.
                import inspect
                allowed = self._budget_check(reservation, amount, prepared.request_digest) if self._budget_check else False
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if allowed is not True:
                    raise DirectorError('BUDGET_PERMISSION_REQUIRED', '视觉调用缺少对应素材的预算许可。')
                frames = prepared.frames[offset:offset + FRAMES_PER_GROUP]
                content = [{'type': 'text', 'text': prompt}]
                for frame in frames:
                    content += [{'type': 'text', 'text': json.dumps({'frame_id': frame.frame_id,
                                'asset_id': frame.asset_id, 'time': frame.time})},
                                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' +
                                 base64.b64encode(frame.jpeg).decode(), 'detail': 'original'}}]
                text_bytes = sum(len(item['text'].encode()) for item in content if item['type'] == 'text')
                # Official image maximum 1024 tokens/image; text upper bound UTF-8 bytes + framing margin.
                if text_bytes + 2048 + len(frames) * 1024 > self.input_limit:
                    raise DirectorError('INPUT_LIMIT', '视觉输入超出本轮费用预留范围。')
                raw, usage = await self.request([{'role': 'user', 'content': content}], reservation)
                records.append(usage)
                result = parse_observations(raw, frames)
                observations.extend(result.observations)
                uncertainties.extend(result.uncertainties)
                if len(result.observations) != len(frames):
                    uncertainties.append('部分采样帧没有可用观察，未据此补写内容。')
                if usage['cost_status'] != 'known':
                    raise DirectorError('COST_UNKNOWN', '视觉用量或计费时段未核对，已暂停后续组。')
            uncertainties += ['仅观察稀疏静态帧，未分析音轨；时间为抽帧位置。', '画面描述全部待人工核对，不推断人物身份或完整事件。']
            return {'result': VisionResult(observations=observations, uncertainties=list(dict.fromkeys(uncertainties)),
                                           coverage=list(prepared.coverage)), 'usage': aggregate(records)}
        except DirectorError as exc:
            if exc.usage and exc.usage.get('cost_status') != 'not_sent':
                records.append(exc.usage)
            exc.usage = aggregate(records)
            raise
        except Exception:
            raise DirectorError('MODEL_REQUEST_FAILED', '视觉准备或调用失败。', usage=aggregate(records)) from None
