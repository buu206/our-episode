"""MiniMax ASR with source-bound timestamps and persistent budget permission.

No automatic retries, remote file URLs, transcription hints or speaker identity
claims. Local chunks remain mono PCM16/16 kHz and at most 30 seconds each.
"""
import asyncio
from decimal import Decimal, ROUND_CEILING
import inspect
import json
import math
import re

from .director import DirectorError, _unique_object, _invalid_constant
from .provider_config import guard
from .speech import CloudSpeech, SpeechError, _chunk_data, _combine, _usage

MODEL = 'asr-1.0'
ENDPOINT = 'https://api.minimax.cn/v1/speech_to_text'
MAX_RESPONSE_BYTES = 1_000_000
MICROYUAN_PER_HOUR = 2_500_000  # Official standard price checked 2026-11-12.


def cost_yuan(seconds):
    units = (Decimal(str(seconds)) * MICROYUAN_PER_HOUR / 3600).to_integral_value(rounding=ROUND_CEILING)
    return int(units) / 1e6


def asr_estimate(chunks):
    # Whole-second upper bound plus 2x headroom; billing uses provider duration.
    return round(sum(cost_yuan(math.ceil(c.duration)) * 2 for c, _ in _chunk_data(chunks)), 6)


def usage(**kwargs):
    return {**_usage(MODEL, **kwargs), 'provider': 'minimax', 'pricing_revision': '2026-11-12'}


def combined(records):
    return {**_combine(records, MODEL), 'provider': 'minimax', 'pricing_revision': '2026-11-12'}


def parse_transcription(value, chunk):
    """Keep provider timings; reject missing coverage rather than align by guess."""
    try:
        text, segments, speakers = value['text'], value.get('segments'), value['n_speakers']
        # Actual verbose_json silence response: text="", segments=null,
        # n_speakers=0. Normalise only this explicit no-speech combination;
        # nonempty text must still have complete, validated timestamps.
        if isinstance(text, str) and not text.strip() and segments is None and type(speakers) is int and speakers == 0:
            segments = []
        if (not isinstance(text, str) or len(text) > 20000 or not isinstance(segments, list)
                or len(segments) > 3000 or type(speakers) is not int or not 0 <= speakers <= 100):
            raise ValueError('envelope')
        words, previous, found = [], -1, set()
        for index, segment in enumerate(segments):
            start, end, word, speaker = (segment[k] for k in ('start', 'end', 'text', 'speaker'))
            if (type(segment.get('id')) is not int or segment['id'] != index
                    or type(start) not in (int, float) or type(end) not in (int, float)
                    or not math.isfinite(start) or not math.isfinite(end)
                    or not 0 <= start < end <= chunk.duration + .001 or start >= chunk.duration or start < previous
                    or not isinstance(word, str) or not word.strip() or len(word) > 1000
                    or not isinstance(speaker, str) or not re.fullmatch(r'S[1-9][0-9]{0,2}', speaker)):
                raise ValueError('segment')
            # IDs are local to this request, not identities across chunks/assets.
            words.append({'asset_id': chunk.asset_id, 'start': chunk.offset + start,
                          'end': chunk.offset + min(end, chunk.duration), 'text': word,
                          'speaker': speaker, 'speaker_scope': f'{chunk.asset_id}:{chunk.offset}'})
            previous = start
            found.add(speaker)
        compact = lambda s: ''.join(s.split())
        if compact(text) != compact(''.join(row['text'] for row in words)):
            raise SpeechError('INCOMPLETE_TIMESTAMPS', '转写全文未与时间戳完整对应，不能生成猜测字幕。')
        if len(found) != speakers or (bool(text.strip()) != bool(words)):
            raise ValueError('speakers or empty text')
        return words
    except SpeechError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError):
        raise SpeechError('INVALID_ASR_RESULT', '转写文本、来源时间戳或说话人结构未通过校验。') from None


class MiniMaxASR(CloudSpeech):
    def _guard(self):
        try:
            guard(self._config, 'MINIMAX')
        except DirectorError as exc:
            raise SpeechError(exc.code, exc.message) from None
        if self._config.get('MINIMAX_ASR_MODEL', MODEL) != MODEL:
            raise SpeechError('MODEL_NOT_ALLOWED', '当前MiniMax转写仅适配asr-1.0。')

    async def _permit(self, reservation, required, current):
        try:
            if reservation is None or self._budget_check is None:
                raise ValueError('missing')
            result = self._budget_check(reservation, required=required)
            if inspect.isawaitable(result):
                result = await result
            if result is not True:
                raise ValueError('denied')
        except Exception:
            raise SpeechError('BUDGET_PERMISSION_REQUIRED', '转写需要足额的持久预算预留。', usage=current) from None

    async def transcribe(self, chunks, *, reservation=None):
        self._guard()
        prepared = _chunk_data(chunks)
        required = asr_estimate(chunks)
        records, words = [], []
        current = usage(cost=0, status='not_sent')
        await self._permit(reservation, required, current)
        client = self._client(current)
        try:
            async with client:
                for chunk, data in prepared:
                    self._guard()
                    await self._permit(reservation, required, current)
                    # From this point, interruption retains the unresolved reservation.
                    current = usage()
                    async with asyncio.timeout(90):
                        async with client.stream('POST', ENDPOINT, follow_redirects=False,
                                headers={'Authorization': 'Bearer ' + self._config['MINIMAX_API_KEY']},
                                data={'model': MODEL, 'response_format': 'verbose_json',
                                      'timestamp_level': 'word', 'stream': 'false'},
                                files={'file': ('audio.wav', data, 'audio/wav')}) as response:
                            if response.status_code != 200:
                                code, message = {
                                    401: ('ASR_AUTH_FAILED', 'MiniMax转写鉴权失败。'),
                                    402: ('ASR_BALANCE_REQUIRED', 'MiniMax转写可用额度不足。'),
                                }.get(response.status_code, ('SPEECH_HTTP_ERROR', 'MiniMax转写请求失败。'))
                                raise SpeechError(code, message + '未自动重发，费用待核对。')
                            body = bytearray()
                            async for block in response.aiter_bytes():
                                body.extend(block)
                                if len(body) > MAX_RESPONSE_BYTES:
                                    raise SpeechError('INVALID_ASR_RESULT', '转写响应过大，未接受结果。')
                    value = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                    if not isinstance(value, dict) or value.get('error') or value.get('type') == 'error':
                        raise ValueError('response')
                    seconds, trace = value.get('duration'), value.get('trace_id')
                    current['request_id'] = trace if isinstance(trace, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', trace) else None
                    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                            or seconds <= 0 or abs(seconds - chunk.duration) > .05 or not current['request_id']):
                        raise SpeechError('COST_UNKNOWN', '转写计费时长缺失或与输入不符，暂停处理并保留预算。')
                    current.update(seconds=seconds, cost_yuan=cost_yuan(seconds), cost_status='known',
                                   usage_basis='provider_duration')
                    try:
                        words.extend(parse_transcription(value, chunk))
                    except SpeechError:
                        # Diagnose protocol shape without persisting transcript
                        # text in usage/error logs or guessing missing words.
                        segments = value.get('segments')
                        current['validation'] = {
                            'offset': chunk.offset, 'chunk_duration': chunk.duration,
                            'has_segments': 'segments' in value,
                            'segments_type': type(segments).__name__,
                            'segment_count': len(segments) if isinstance(segments, list) else None,
                            'text_length': len(value['text']) if isinstance(value.get('text'), str) else None,
                            'speaker_count': value.get('n_speakers') if type(value.get('n_speakers')) is int else None,
                        }
                        raise
                    records.append(current)
                    current = usage(cost=0, status='not_sent')
            return {'result': words, 'usage': combined(records)}
        except SpeechError as exc:
            exc.usage = combined([*records, exc.usage or current])
            raise
        except Exception:
            raise SpeechError('ASR_REQUEST_FAILED', '转写超时或响应无效，未自动重发，请核对任务费用。',
                              usage=combined([*records, current])) from None

    async def synthesize(self, text, output_path, *, reservation=None):
        raise SpeechError('MODEL_NOT_ALLOWED', '转写适配器不提供旁白。')
