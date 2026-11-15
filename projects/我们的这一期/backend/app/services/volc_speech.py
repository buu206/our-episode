"""Bounded Volcengine ASR 2.0 WebSocket and TTS 2.0 SSE adapters.

No public media URLs, automatic retries, voice cloning or provider fallback.
"""
import asyncio
import base64
from decimal import Decimal, ROUND_CEILING
import gzip
import io
import json
import math
import os
import re
import struct
import wave
from uuid import uuid4
import zlib

import httpx
from websockets.asyncio.client import connect

from .director import DirectorError, _unique_object, _invalid_constant
from .provider_config import guard
from .speech import CloudSpeech, SpeechError, _chunk_data, _local_path, _wav_info, _usage, _combine

ASR_MODEL = 'volc.seedasr.sauc.duration'
TTS_MODEL = 'seed-tts-2.0'
ASR_URL = 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream'
TTS_URL = 'https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse'
SPEAKER = 'zh_female_vv_uranus_bigtts'
MAX_PACKET = 200_000


class FixedEndpointConnect(connect):
    def process_redirect(self, exc):
        # websockets normally forwards X-Api-Key across redirects. Refuse all.
        return exc


def safe_id(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value) else None


def usage(model, **kwargs):
    return {**_usage(model, **kwargs), 'provider': 'volc', 'pricing_revision': '2026-11-10'}


def asr_estimate(chunks):
    return float((sum(Decimal(math.ceil(c.duration)) for c, _ in _chunk_data(chunks)) / 3600)
                 .quantize(Decimal('.000001'), rounding=ROUND_CEILING))


def text_count(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 120:
        raise SpeechError('INVALID_TTS_TEXT', '本轮旁白需要1至120字符并由用户确认。')
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise SpeechError('INVALID_TTS_TEXT', '旁白包含无效字符。') from None
    return len(text)


def tts_estimate(text):
    return text_count(text) * .0003


def encode_packet(data, *, audio=False, final=False):
    payload = gzip.compress(data, mtime=0)
    return bytes((0x11, (0x20 if audio else 0x10) | (2 if final else 0),
                  0x01 if audio else 0x11, 0)) + struct.pack('>I', len(payload)) + payload


def decode_packet(data):
    try:
        if not isinstance(data, bytes) or not 8 <= len(data) <= MAX_PACKET or data[0] != 0x11:
            raise ValueError('header')
        kind, flags, serialization, compression = data[1] >> 4, data[1] & 15, data[2] >> 4, data[2] & 15
        if kind == 15:
            raise SpeechError('ASR_PROVIDER_ERROR', '转写服务返回错误，费用待核对。')
        if kind != 9 or flags not in (0, 1, 2, 3) or serialization != 1 or compression not in (0, 1):
            raise ValueError('protocol')
        pos = 4
        if flags & 1:
            sequence = struct.unpack('>i', data[pos:pos + 4])[0]
            if (sequence < 0) != bool(flags & 2):
                raise ValueError('sequence')
            pos += 4
        size = struct.unpack('>I', data[pos:pos + 4])[0]
        payload = data[pos + 4:]
        if size != len(payload):
            raise ValueError('length')
        if compression:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            payload = decoder.decompress(payload, MAX_PACKET + 1)
            if not decoder.eof or decoder.unused_data or len(payload) > MAX_PACKET:
                raise ValueError('compressed size')
        result = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if not isinstance(result, dict):
            raise ValueError('result')
        return result, bool(flags & 2)
    except SpeechError:
        raise
    except Exception:
        raise SpeechError('INVALID_ASR_RESULT', '转写数据包不完整或格式无效。') from None


def compact(text):
    # Punctuation may be absent from word objects; lexical characters must match.
    import unicodedata
    return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))


def parse_final(envelope, chunk):
    try:
        result = envelope['result']
        if not isinstance(result, dict) or not isinstance(result.get('text'), str) or len(result['text']) > 20000:
            raise ValueError('text')
        utterances = result.get('utterances', [])
        if not isinstance(utterances, list) or len(utterances) > 1000:
            raise ValueError('utterances')
        output, sentences, previous = [], [], 0
        for sentence in utterances:
            begin, end, text = sentence['start_time'], sentence['end_time'], sentence['text']
            if (sentence.get('definite') is not True or type(begin) is not int or type(end) is not int
                    or not previous <= begin < end <= math.ceil(chunk.duration * 1000)
                    or not isinstance(text, str) or not compact(text)):
                raise ValueError('sentence')
            words = sentence['words']
            if not isinstance(words, list) or not 1 <= len(words) <= 3000:
                raise ValueError('words')
            emitted, lexical, pending, cursor = [], '', '', begin
            for word in words:
                start, finish, value = word['start_time'], word['end_time'], word['text']
                if (type(start) is not int or type(finish) is not int or not cursor <= start <= finish <= end
                        or not isinstance(value, str) or not value or len(value) > 1000):
                    raise ValueError('word')
                lexical += value
                if start == finish:
                    # Merge zero-duration characters with an adjacent timed word;
                    # never invent a new time interval for an untimed character.
                    if emitted:
                        emitted[-1]['text'] += value
                    else:
                        pending += value
                else:
                    emitted.append({'asset_id': chunk.asset_id, 'start': chunk.offset + start / 1000,
                                    'end': chunk.offset + min(finish / 1000, chunk.duration), 'text': pending + value})
                    pending = ''
                cursor = finish
            if pending or not emitted or compact(lexical) != compact(text):
                raise ValueError('missing word alignment')
            sentences.append(text)
            output.extend(emitted)
            previous = end
        if compact(''.join(sentences)) != compact(result['text']):
            raise ValueError('missing sentence')
        return output
    except Exception:
        raise SpeechError('INCOMPLETE_TIMESTAMPS', '转写全文与最终句词时间戳未完整对应，已停止。') from None


class VolcSpeech(CloudSpeech):
    def __init__(self, config, *, capability='asr', client_factory=None, budget_check=None,
                 ws_factory=None, sleep=None):
        super().__init__(config, client_factory=client_factory, budget_check=budget_check)
        self.capability = capability
        self._ws_factory = ws_factory or FixedEndpointConnect
        self._sleep = sleep or asyncio.sleep

    def _guard(self):
        try:
            guard(self._config, 'VOLC')
        except DirectorError as exc:
            raise SpeechError(exc.code, exc.message) from None
        field, expected = ('VOLC_ASR_RESOURCE', ASR_MODEL) if self.capability == 'asr' else ('VOLC_TTS_RESOURCE', TTS_MODEL)
        if self.capability not in ('asr', 'tts') or self._config.get(field, expected) != expected:
            raise SpeechError('MODEL_NOT_ALLOWED', '豆包语音资源型号未获准使用。')
        if self.capability == 'tts' and self._config.get('VOLC_TTS_SPEAKER', SPEAKER) != SPEAKER:
            raise SpeechError('VOICE_NOT_ALLOWED', '当前只允许已选的系统音色，不使用声音复刻。')

    async def _one_chunk(self, chunk, data, reservation):
        current = usage(ASR_MODEL, cost=0, status='not_sent')
        await self._budget(reservation, current)
        self._guard()
        request_id = uuid4().hex
        try:
            connection = self._ws_factory(ASR_URL, additional_headers={
                'X-Api-Key': self._config['VOLC_API_KEY'], 'X-Api-Resource-Id': ASR_MODEL,
                'X-Api-Request-Id': request_id, 'X-Api-Connect-Id': request_id, 'X-Api-Sequence': '-1'},
                proxy=None, compression=None, max_size=MAX_PACKET, max_queue=16,
                open_timeout=10, close_timeout=3, ping_timeout=10)
        except Exception:
            raise SpeechError('CLIENT_INIT_FAILED', '转写客户端初始化失败，尚未发送请求。', usage=current) from None
        try:
            current = usage(ASR_MODEL)
            async with asyncio.timeout(chunk.duration + 35):
                async with connection as ws:
                    current['request_id'] = safe_id(ws.response.headers.get('x-tt-logid'))
                    with wave.open(io.BytesIO(data), 'rb') as source:
                        pcm = source.readframes(source.getnframes())
                    body = {'user': {'uid': 'episode-local'}, 'audio': {'format': 'pcm', 'codec': 'raw',
                            'rate': 16000, 'bits': 16, 'channel': 1}, 'request': {'model_name': 'bigmodel',
                            'show_utterances': True, 'result_type': 'full', 'enable_ddc': False,
                            'enable_itn': False, 'enable_punc': False}}
                    await self._budget(reservation, current)
                    await ws.send(encode_packet(json.dumps(body).encode()))
                    all_sent = False
                    async def send_audio():
                        nonlocal all_sent
                        for offset in range(0, len(pcm), 6400):
                            if offset:
                                await self._sleep(.2)
                            if offset % 32000 == 0:
                                self._guard()
                                await self._budget(reservation, current)
                            block = pcm[offset:offset + 6400]
                            final = offset + len(block) == len(pcm)
                            await ws.send(encode_packet(block, audio=True, final=final))
                            if final:
                                all_sent = True
                    async def receive():
                        stable = {}
                        for _ in range(1000):
                            envelope, final = decode_packet(await ws.recv())
                            result = envelope.get('result')
                            if isinstance(result, dict):
                                for item in result.get('utterances', []):
                                    if item.get('definite') is True:
                                        key = (item.get('start_time'), item.get('end_time'))
                                        if key in stable and stable[key] != item:
                                            raise SpeechError('INVALID_ASR_RESULT', '已确定的转写分句发生变化。')
                                        stable[key] = item
                            if final:
                                if not all_sent or not isinstance(result, dict):
                                    raise SpeechError('INCOMPLETE_ASR', '音频尚未发完或缺少最终转写结果。')
                                final_items = {(x.get('start_time'), x.get('end_time')): x for x in result.get('utterances', [])}
                                if any(final_items.get(k) != v for k, v in stable.items()):
                                    raise SpeechError('INCOMPLETE_ASR', '最终转写丢失已确定分句。')
                                return envelope
                        raise SpeechError('INVALID_ASR_RESULT', '转写返回包数量超过限制。')
                    sender = asyncio.create_task(send_audio())
                    receiver = asyncio.create_task(receive())
                    try:
                        _, envelope = await asyncio.gather(sender, receiver)
                    finally:
                        for task in (sender, receiver):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(sender, receiver, return_exceptions=True)
                    duration = envelope.get('audio_info', {}).get('duration')
                    if type(duration) is int and abs(duration - chunk.duration * 1000) <= 1 and current['request_id']:
                        seconds = math.ceil(duration / 1000)
                        current.update(seconds=seconds, cost_yuan=float(Decimal(seconds) / 3600),
                                       cost_status='known', usage_basis='provider_audio_duration')
                    words = parse_final(envelope, chunk)
                    return words, current
        except SpeechError as exc:
            exc.usage = current
            raise
        except Exception:
            raise SpeechError('ASR_STREAM_FAILED', '转写连接中断、超时或数据无效，费用待核对；不会自动重发。', usage=current) from None

    async def transcribe(self, chunks, *, reservation=None):
        self._guard()
        if self.capability != 'asr':
            raise SpeechError('MODEL_NOT_ALLOWED', '该适配器未配置转写能力。')
        prepared = _chunk_data(chunks)
        records, words = [], []
        try:
            for chunk, data in prepared:
                result, current = await self._one_chunk(chunk, data, reservation)
                records.append(current)
                words.extend(result)
                if current['cost_status'] != 'known':
                    raise SpeechError('COST_UNKNOWN', '转写用量未核对，已暂停后续音频块。')
            combined = _combine(records, ASR_MODEL)
            combined['provider'] = 'volc'
            return {'result': words, 'usage': combined}
        except SpeechError as exc:
            exc.usage = _combine([*records, *([exc.usage] if exc.usage else [])], ASR_MODEL)
            exc.usage['provider'] = 'volc'
            raise

    async def synthesize(self, text, output_path, *, reservation=None):
        self._guard()
        if self.capability != 'tts':
            raise SpeechError('MODEL_NOT_ALLOWED', '该适配器未配置配音能力。')
        count = text_count(text)
        target = _local_path(output_path, existing=False)
        if target.exists() or not target.parent.is_dir() or target.suffix.lower() != '.wav':
            raise SpeechError('INVALID_AUDIO_PATH', '需要未使用的受控WAV文件位置。')
        current = usage(TTS_MODEL, cost=0, status='not_sent')
        await self._budget(reservation, current)
        client = self._client(current)
        pcm, terminal = bytearray(), False
        try:
            async with client, asyncio.timeout(100):
                await self._budget(reservation, current)
                current = usage(TTS_MODEL)
                async with client.stream('POST', TTS_URL, headers={
                    'X-Api-Key': self._config['VOLC_API_KEY'], 'X-Api-Resource-Id': TTS_MODEL,
                    'X-Api-Request-Id': uuid4().hex, 'X-Control-Require-Usage-Tokens-Return': 'text_words'},
                    json={'user': {'uid': 'episode-local'}, 'req_params': {'text': text,
                          'speaker': SPEAKER, 'audio_params': {'format': 'pcm', 'sample_rate': 24000}}},
                    follow_redirects=False) as response:
                    current['request_id'] = safe_id(response.headers.get('x-tt-logid'))
                    if response.status_code != 200:
                        raise SpeechError('SPEECH_HTTP_ERROR', '配音请求失败，费用待核对。')
                    buffer, total = b'', 0
                    async for block in response.aiter_bytes():
                        total += len(block)
                        buffer += block
                        if total > 2_200_000 or len(buffer) > 2_000_000:
                            raise SpeechError('INVALID_TTS_RESULT', '配音流超过本轮限制。')
                        while b'\n\n' in buffer.replace(b'\r\n', b'\n'):
                            buffer = buffer.replace(b'\r\n', b'\n')
                            event, buffer = buffer.split(b'\n\n', 1)
                            payload = b'\n'.join(line[5:].lstrip() for line in event.split(b'\n') if line.startswith(b'data:'))
                            if not payload:
                                continue
                            if terminal:
                                raise SpeechError('INVALID_TTS_RESULT', '配音终态后出现额外内容。')
                            item = json.loads(payload, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                            code = item.get('code')
                            if type(code) is not int or code not in (0, 20000000):
                                raise SpeechError('TTS_PROVIDER_ERROR', '配音服务返回错误，费用待核对。')
                            if item.get('data'):
                                pcm.extend(base64.b64decode(item['data'], validate=True))
                                if len(pcm) > 24000 * 2 * 30:
                                    raise SpeechError('INVALID_TTS_RESULT', '配音超过30秒上限。')
                            if code == 20000000:
                                terminal = True
                                units = item.get('usage', {}).get('text_words')
                                if type(units) is int and 0 < units <= count and current['request_id']:
                                    current.update(characters=units, cost_yuan=units * .0003,
                                                   cost_status='known', usage_basis='provider_characters')
                    if not terminal or buffer.strip() or not pcm or len(pcm) % 2:
                        raise SpeechError('INCOMPLETE_TTS', '配音缺少完整音频或成功终态。')
            buffer = io.BytesIO()
            with wave.open(buffer, 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(pcm)
            data = buffer.getvalue()
            duration = _wav_info(data, asr=False)
            created = False
            try:
                with target.open('xb') as handle:
                    created = True
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                if created:
                    target.unlink(missing_ok=True)
                raise
            return {'result': {'path': str(target), 'duration': duration}, 'usage': current}
        except SpeechError as exc:
            exc.usage = current
            raise
        except Exception:
            raise SpeechError('TTS_STREAM_FAILED', '配音流中断、超时或结果无效；费用需核对。', usage=current) from None
