"""MiniMax system-voice TTS. Disabled by default; no retries or URL downloads."""
import asyncio
import inspect
import json
import math
import os
import re
import wave
import io

from .director import DirectorError, _unique_object, _invalid_constant
from .provider_config import guard
from .speech import CloudSpeech, SpeechError, _local_path, _wav_info, _usage

MODEL = 'speech-2.8-hd'
ENDPOINT = 'https://api.minimax.cn/v1/t2a_v2'
VOICES = frozenset(('male-qn-daxuesheng', 'male-qn-qingse', 'female-yujie'))
EMOTIONS = frozenset(('auto', 'happy', 'calm', 'surprised', 'sad', 'angry', 'fearful', 'disgusted'))
PRICE_MICROYUAN = 350  # CNY 3.50 / 10,000 billing characters; Chinese counts twice.
MAX_AUDIO_BYTES = 1_500_000  # mono PCM16, 24 kHz, <=30 s including WAV header
MAX_RESPONSE_BYTES = MAX_AUDIO_BYTES * 2 + 32_000


def text_units_bound(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 120:
        raise SpeechError('INVALID_TTS_TEXT', '本轮旁白需要1至120字符并由用户确认。')
    try:
        text.encode('utf-8')
    except UnicodeError:
        raise SpeechError('INVALID_TTS_TEXT', '旁白包含无效字符。') from None
    # Reserve a conservative maximum, settle only from provider usage_characters.
    return len(text) * 2


def tts_estimate(text):
    return text_units_bound(text) * PRICE_MICROYUAN / 1e6


def usage(**kwargs):
    return {**_usage(MODEL, **kwargs), 'provider': 'minimax', 'pricing_revision': '2026-11-12'}


class MiniMaxSpeech(CloudSpeech):
    def _voice(self):
        voice = self._config.get('MINIMAX_TTS_VOICE', 'male-qn-daxuesheng')
        emotion = self._config.get('MINIMAX_TTS_EMOTION', 'happy')
        if not isinstance(voice, str) or voice not in VOICES:
            raise SpeechError('VOICE_NOT_ALLOWED', '请选择已核对的MiniMax系统音色，不使用复刻或设计音色。')
        try:
            raw_speed = self._config.get('MINIMAX_TTS_SPEED', '1.15')
            raw_pitch = self._config.get('MINIMAX_TTS_PITCH', '0')
            if isinstance(raw_speed, bool) or isinstance(raw_pitch, bool):
                raise ValueError('boolean')
            speed = float(raw_speed)
            pitch = float(raw_pitch)
            if not math.isfinite(speed) or not .5 <= speed <= 2 or not -12 <= pitch <= 12 or not pitch.is_integer():
                raise ValueError('range')
            if not isinstance(emotion, str) or emotion not in EMOTIONS:
                raise ValueError('emotion')
        except (TypeError, ValueError):
            raise SpeechError('INVALID_VOICE_SETTINGS', '旁白语速、音调或情绪配置无效。') from None
        result = {'voice_id': voice, 'speed': speed, 'pitch': int(pitch), 'vol': 1}
        if emotion != 'auto':
            result['emotion'] = emotion
        return result

    def _guard(self):
        try:
            guard(self._config, 'MINIMAX')
        except DirectorError as exc:
            raise SpeechError(exc.code, exc.message) from None
        if self._config.get('MINIMAX_TTS_MODEL', MODEL) != MODEL:
            raise SpeechError('MODEL_NOT_ALLOWED', '当前仅适配MiniMax Speech 2.8 HD。')
        self._voice()

    async def _permit(self, reservation, required, current):
        try:
            if reservation is None or self._budget_check is None:
                raise ValueError('missing')
            permitted = self._budget_check(reservation, required=required)
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if permitted is not True:
                raise ValueError('denied')
        except Exception:
            raise SpeechError('BUDGET_PERMISSION_REQUIRED', '配音需要足额的持久预算预留。', usage=current) from None

    async def transcribe(self, chunks, *, reservation=None):
        raise SpeechError('MODEL_NOT_ALLOWED', '当前MiniMax适配器仅用于旁白，不提供转写。')

    async def synthesize(self, text, output_path, *, reservation=None):
        self._guard()
        bound = text_units_bound(text)
        target = _local_path(output_path, existing=False)
        if target.exists() or not target.parent.is_dir() or target.suffix.lower() != '.wav':
            raise SpeechError('INVALID_AUDIO_PATH', '需要未使用的受控WAV文件位置。')
        current = usage(cost=0, status='not_sent')
        await self._permit(reservation, tts_estimate(text), current)
        client = self._client(current)
        try:
            async with client, asyncio.timeout(90):
                self._guard()
                await self._permit(reservation, tts_estimate(text), current)
                current = usage()
                async with client.stream('POST', ENDPOINT, follow_redirects=False,
                        headers={'Authorization': 'Bearer ' + self._config['MINIMAX_API_KEY']},
                        json={'model': MODEL, 'text': text, 'stream': False, 'output_format': 'hex',
                              'voice_setting': self._voice(), 'audio_setting': {
                                  'format': 'wav', 'sample_rate': 24000, 'channel': 1},
                              'subtitle_enable': False}) as response:
                    if response.status_code != 200:
                        raise SpeechError('SPEECH_HTTP_ERROR', 'MiniMax配音请求失败；费用待核对，不自动重发。')
                    body = bytearray()
                    async for block in response.aiter_bytes():
                        body.extend(block)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise SpeechError('INVALID_TTS_RESULT', '配音响应超过本轮大小限制。')
                value = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
                if not isinstance(value, dict):
                    raise ValueError('envelope')
                trace = value.get('trace_id')
                current['request_id'] = trace if isinstance(trace, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', trace) else None
                base = value.get('base_resp')
                code = base.get('status_code') if isinstance(base, dict) else None
                if type(code) is int and 0 <= code <= 99999:
                    current['provider_status_code'] = code
                if code == 1004:
                    raise SpeechError('TTS_AUTH_FAILED', 'MiniMax密钥鉴权失败，请核对密钥所属平台与类型；未自动重发。')
                if code == 1008:
                    raise SpeechError('TTS_BALANCE_REQUIRED', 'MiniMax账户可用余额不足，请核对控制台；未自动重发。')
                if not isinstance(base, dict) or type(base.get('status_code')) is not int or base['status_code'] != 0:
                    raise SpeechError('TTS_PROVIDER_ERROR', 'MiniMax配音未成功；请核对账户与服务状态，费用待核对。')
                extra = value.get('extra_info')
                if not isinstance(extra, dict):
                    raise ValueError('extra_info')
                units = extra.get('usage_characters')
                voice_count = extra.get('usage_voice_count', 0)
                if (type(units) is int and 0 < units <= bound and current['request_id']
                        and type(voice_count) is int and voice_count == 0):
                    current.update(characters=units, cost_yuan=units * PRICE_MICROYUAN / 1e6,
                                   cost_status='known', usage_basis='provider_characters')
                else:
                    raise SpeechError('COST_UNKNOWN', '配音计费用量缺失、超出预留或出现额外音色费用，暂停后续处理。')
                data = value.get('data')
                if not isinstance(data, dict) or type(data.get('status')) is not int or data['status'] != 2:
                    raise SpeechError('INCOMPLETE_TTS', '配音缺少成功终态。')
                encoded = data.get('audio')
                if (not isinstance(encoded, str) or not encoded or len(encoded) % 2
                        or len(encoded) > MAX_AUDIO_BYTES * 2 or not re.fullmatch(r'[0-9a-fA-F]+', encoded)):
                    raise ValueError('audio hex')
                audio = bytes.fromhex(encoded)
                duration = _wav_info(audio, asr=False)
                with wave.open(io.BytesIO(audio), 'rb') as wav:
                    if wav.getnchannels() != 1 or wav.getframerate() != 24000:
                        raise ValueError('audio settings')
                expected = {'audio_sample_rate': 24000, 'audio_channel': 1, 'audio_size': len(audio)}
                if any(type(extra.get(k)) is not int or extra[k] != v for k, v in expected.items()):
                    raise ValueError('audio metadata')
                if (extra.get('audio_format') != 'wav' or type(extra.get('audio_length')) is not int
                        or abs(extra['audio_length'] / 1000 - duration) > .05):
                    raise ValueError('duration metadata')
            created = False
            try:
                with target.open('xb') as handle:
                    created = True
                    handle.write(audio)
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
            raise SpeechError('TTS_REQUEST_FAILED', '配音超时、响应无效或本地保存失败；不自动重发，请核对任务费用。', usage=current) from None
