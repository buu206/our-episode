"""Disabled-by-default Beijing speech adapters, with local-only audio preparation.

The caller supplies authorised local Paths and persisted budget reservations.
No .env loading, cloud admission changes, public uploads or voice cloning occur.
See 阶段2-1-语音接口核查.md for the verified API contract and unresolved live checks.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import inspect
import io
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
import wave

import httpx

from . import media


ASR_MODEL = "fun-asr-flash-2026-06-15"
TTS_MODEL = "qwen3-tts-flash"
ENDPOINT = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
TTS_AUDIO_HOST = "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
MAX_AUDIO_BYTES = 6_000_000
MAX_CHUNK_SECONDS = 30.0
MAX_TOTAL_SECONDS = 600.0
ASR_MICROYUAN_PER_SECOND = 220
TTS_MICROYUAN_PER_CHARACTER = 80


class SpeechError(Exception):
    def __init__(self, code: str, message: str, *, usage: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.usage = code, message, usage


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    asset_id: str
    offset: float
    duration: float


def _local_path(path: Path, *, existing: bool) -> Path:
    if not isinstance(path, Path) or path.is_symlink():
        raise SpeechError("INVALID_AUDIO_PATH", "需要受控的本地文件路径。")
    resolved = path.resolve()
    if existing and not resolved.is_file():
        raise SpeechError("INVALID_AUDIO_PATH", "本地音频或视频文件不存在。")
    return resolved


def _wav_info(data: bytes, *, asr: bool) -> float:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
            if (audio.getcomptype() != "NONE" or width != 2 or frames <= 0
                    or (asr and (channels != 1 or rate != 16000))
                    or (not asr and (channels not in (1, 2) or rate not in (8000, 16000, 22050, 24000, 44100, 48000)))):
                raise ValueError("unsupported PCM")
            duration = frames / rate
            if not 0 < duration <= (MAX_CHUNK_SECONDS if asr else 30):
                raise ValueError("duration limit")
            if len(audio.readframes(frames)) != frames * channels * width:
                raise ValueError("truncated PCM")
            return duration
    except (wave.Error, EOFError, OSError, ValueError):
        raise SpeechError("INVALID_AUDIO", "音频不可完整解码、格式不符或超过30秒。") from None


def _chunk_data(chunks: list[AudioChunk]) -> list[tuple[AudioChunk, bytes]]:
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 40:
        raise SpeechError("INVALID_AUDIO", "需要1至40个本地音频短块。")
    result, ends, total = [], {}, 0.0
    for chunk in chunks:
        if (not isinstance(chunk, AudioChunk) or not isinstance(chunk.asset_id, str)
                or not chunk.asset_id.strip() or len(chunk.asset_id) > 128
                or type(chunk.offset) not in (float, int) or not 0 <= chunk.offset <= 600
                or type(chunk.duration) not in (float, int) or not 0 < chunk.duration <= MAX_CHUNK_SECONDS
                or chunk.offset + chunk.duration > 600.001
                or chunk.offset < ends.get(chunk.asset_id, 0) - 1e-8):
            raise SpeechError("INVALID_AUDIO", "音频来源或时间范围无效。")
        path = _local_path(chunk.path, existing=True)
        try:
            if path.stat().st_size > MAX_AUDIO_BYTES:
                raise SpeechError("INVALID_AUDIO", "音频短块过大。")
            data = path.read_bytes()
        except OSError:
            raise SpeechError("INVALID_AUDIO", "本地音频短块无法读取。") from None
        duration = _wav_info(data, asr=True)
        if abs(duration - chunk.duration) > 1 / 16000 + 1e-8:
            raise SpeechError("INVALID_AUDIO", "音频实际时长与来源记录不一致。")
        ends[chunk.asset_id] = chunk.offset + duration
        total += duration
        result.append((chunk, data))
    if total > MAX_TOTAL_SECONDS + 1e-8:
        raise SpeechError("INVALID_AUDIO", "本次转写音频总时长超过10分钟。")
    return result


def extract_audio_chunks(source: Path, output_dir: Path, asset_id: str,
                         chunk_seconds: float = MAX_CHUNK_SECONDS) -> list[AudioChunk]:
    """Extract mono 16 kHz PCM locally and split on exact sample boundaries.

    Caller supplies an authorised MP4/MOV and controlled destination directory.
    The source is never changed; generated filenames are random and not overwritten.
    """
    source = _local_path(source, existing=True)
    if (not isinstance(output_dir, Path) or output_dir.is_symlink()
            or not isinstance(asset_id, str) or not asset_id.strip() or len(asset_id) > 128
            or type(chunk_seconds) not in (int, float) or not math.isfinite(chunk_seconds)
            or not 1 <= chunk_seconds <= MAX_CHUNK_SECONDS):
        raise SpeechError("INVALID_AUDIO", "音频提取参数无效。")
    try:
        info = media.probe(source)
    except media.MediaError as exc:
        raise SpeechError(exc.code, exc.message) from None
    if not info["has_audio"]:
        raise SpeechError("NO_AUDIO_TRACK", "该素材没有音轨，不能生成转写。")
    if info["duration"] > MAX_TOTAL_SECONDS:
        raise SpeechError("INVALID_AUDIO", "素材超过10分钟。")
    executable = shutil.which("ffmpeg")
    if executable is None and Path("/opt/homebrew/bin/ffmpeg").is_file():
        executable = "/opt/homebrew/bin/ffmpeg"
    if executable is None:
        raise SpeechError("MEDIA_TOOL_UNAVAILABLE", "本机尚未安装FFmpeg。")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix="speech-extract-", dir=output_dir) as temp:
            full = Path(temp) / "audio.wav"
            command = [executable, "-nostdin", "-v", "error", "-n", "-protocol_whitelist", "file,pipe",
                       "-f", "mov", "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                       "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "2",
                       "-t", str(info["duration"]), str(full)]
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, shell=False, timeout=90, check=False)
            if completed.returncode:
                raise SpeechError("AUDIO_EXTRACTION_FAILED", "本机音频提取失败，源文件未改动。")
            with wave.open(str(full), "rb") as audio:
                if audio.getnframes() / audio.getframerate() > MAX_TOTAL_SECONDS:
                    raise SpeechError("INVALID_AUDIO", "实际音轨超过10分钟。")
                chunk_frames, position = int(chunk_seconds * 16000), 0
                result = []
                while data := audio.readframes(chunk_frames):
                    target = output_dir / f"audio-{uuid4().hex}.wav"
                    with target.open("xb") as handle:
                        created.append(target)
                        with wave.open(handle, "wb") as part:
                            part.setnchannels(1)
                            part.setsampwidth(2)
                            part.setframerate(16000)
                            part.writeframes(data)
                    frames = len(data) // 2
                    result.append(AudioChunk(target, asset_id, position / 16000, frames / 16000))
                    position += frames
            _chunk_data(result)
            return result
    except SpeechError:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError, wave.Error, EOFError):
        for path in created:
            path.unlink(missing_ok=True)
        raise SpeechError("AUDIO_EXTRACTION_FAILED", "本机音频提取失败，源文件未改动。") from None


def asr_reservation_yuan(chunks: list[AudioChunk]) -> float:
    return sum(math.ceil(chunk.duration) for chunk, _ in _chunk_data(chunks)) * 2 * ASR_MICROYUAN_PER_SECOND / 1e6


def billable_characters(text: str) -> int:
    """Provider price rule: Han ideographs count twice, other characters once."""
    if not isinstance(text, str) or not text.strip() or len(text) > 600:
        raise SpeechError("INVALID_TTS_TEXT", "旁白需要1至600个文字字符，并先由用户确认。")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise SpeechError("INVALID_TTS_TEXT", "旁白包含无效字符。") from None
    def han(char):
        value = ord(char)
        return (0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF
                or 0xF900 <= value <= 0xFAFF or 0x20000 <= value <= 0x2FA1F
                or 0x30000 <= value <= 0x3347F)
    return sum(2 if han(char) else 1 for char in text)


def tts_reservation_yuan(text: str) -> float:
    return billable_characters(text) * 2 * TTS_MICROYUAN_PER_CHARACTER / 1e6


def _usage(model: str, *, cost=None, status="unknown", request_id=None, retries=0, seconds=None, characters=None) -> dict:
    return {"model": model, "input_tokens": None, "output_tokens": None, "cost_yuan": cost,
            "cost_status": status, "request_id": request_id, "retries": retries,
            "seconds": seconds, "characters": characters}


def _combine(records: list[dict], model: str) -> dict:
    known = all(item["cost_yuan"] is not None for item in records)
    result = _usage(model, cost=round(sum(item["cost_yuan"] for item in records), 6) if known else None,
                    status="known" if known else "unknown", retries=sum(item["retries"] for item in records),
                    request_id=next((item["request_id"] for item in reversed(records) if item["request_id"]), None),
                    seconds=sum(item["seconds"] or 0 for item in records) if known else None)
    result["requests"] = records
    return result


def parse_transcription(envelope: dict, chunk: AudioChunk) -> list[dict]:
    """Require stable word timestamps covering all returned text; never invent alignment."""
    try:
        output = envelope["output"]
        text, sentence = output["text"], output.get("sentence")
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError("invalid text")
        if text == "" and (sentence is None or (sentence.get("text") == "" and sentence.get("words") == [])):
            return []
        if (not isinstance(sentence, dict) or sentence.get("sentence_end") is not True
                or not isinstance(sentence.get("words"), list) or not sentence["words"]
                or len(sentence["words"]) > 3000):
            raise ValueError("incomplete timestamps")
        begin, end = sentence["begin_time"], sentence["end_time"]
        if type(begin) is not int or type(end) is not int or not 0 <= begin < end <= math.ceil(chunk.duration * 1000):
            raise ValueError("invalid sentence range")
        words, combined, previous = [], "", begin
        for word in sentence["words"]:
            start, finish, value, punctuation = word["begin_time"], word["end_time"], word["text"], word["punctuation"]
            if (word.get("fixed") is not True or type(start) is not int or type(finish) is not int
                    or not previous <= start < finish <= end or start >= chunk.duration * 1000
                    or not isinstance(value, str) or not value
                    or not isinstance(punctuation, str) or len(value) > 1000 or len(punctuation) > 20):
                raise ValueError("invalid word")
            words.append({"asset_id": chunk.asset_id, "start": chunk.offset + start / 1000,
                          "end": chunk.offset + min(finish / 1000, chunk.duration), "text": value + punctuation})
            combined += value + punctuation
            previous = finish
        compact = lambda value: "".join(value.split())
        if compact(text) != compact(sentence["text"]) or compact(text) != compact(combined):
            raise SpeechError("INCOMPLETE_TIMESTAMPS", "转写全文与词级时间戳未完整对应，不能生成猜测字幕。")
        return words
    except SpeechError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SpeechError("INVALID_ASR_RESULT", "转写结果或时间戳未通过校验。") from None


class CloudSpeech:
    def __init__(self, config: dict, *, client_factory=None, budget_check=None):
        self._config = dict(config)
        self._client_factory, self._budget_check = client_factory, budget_check

    def _guard(self):
        for key, code in (("CLOUD_ENABLED", "CLOUD_DISABLED"), ("CLOUD_RETENTION_VERIFIED", "RETENTION_UNVERIFIED"),
                          ("CLOUD_REGION_VERIFIED", "REGION_UNVERIFIED")):
            if self._config.get(key) is not True:
                raise SpeechError(code, "云语音尚未通过启用、留存或境内处理准入检查。")
        base_url = self._config.get("MODEL_BASE_URL", "")
        if not isinstance(base_url, str) or base_url.rstrip("/") not in (
                "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://dashscope.aliyuncs.com/api/v1"):
            raise SpeechError("ENDPOINT_NOT_ALLOWED", "语音地址不在已核实的北京白名单中。")
        key = self._config.get("MODEL_API_KEY")
        if not isinstance(key, str) or not key or any(char.isspace() for char in key):
            raise SpeechError("KEY_NOT_CONFIGURED", "尚未配置有效模型密钥。")
        if self._config.get("ASR_MODEL", ASR_MODEL) != ASR_MODEL or self._config.get("TTS_MODEL", TTS_MODEL) != TTS_MODEL:
            raise SpeechError("MODEL_NOT_ALLOWED", "语音模型型号未获准使用。")

    def ready(self) -> tuple[bool, str]:
        try:
            self._guard()
            return True, "语音适配接口已配置；仍需每次预算许可与真实效果验证。"
        except SpeechError as exc:
            return False, exc.message

    async def _budget(self, reservation, usage):
        try:
            if reservation is None or self._budget_check is None:
                raise ValueError("missing permission")
            permitted = self._budget_check(reservation)
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if permitted is not True:
                raise ValueError("not permitted")
        except Exception:
            raise SpeechError("BUDGET_PERMISSION_REQUIRED", "语音调用需要有效的持久预算预留。", usage=usage) from None

    def _client(self, unsent):
        try:
            return self._client_factory() if self._client_factory else httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=10), trust_env=False, follow_redirects=False)
        except Exception:
            raise SpeechError("CLIENT_INIT_FAILED", "语音客户端初始化失败，尚未发送请求。", usage=unsent) from None

    async def _post(self, client, body, reservation, model, unit_name, max_units, price):
        uncertain, usage = False, _usage(model, cost=0, status="not_sent")
        for attempt in range(2):
            await self._budget(reservation, usage)
            usage = _usage(model, retries=attempt)
            try:
                response = await client.post(ENDPOINT, json=body, headers={
                    "Authorization": "Bearer " + self._config["MODEL_API_KEY"], "X-DashScope-SSE": "disable"},
                    follow_redirects=False)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                uncertain = True
                if attempt == 0:
                    continue
                raise SpeechError("SPEECH_CONNECTION_FAILED", "语音连接失败，费用未知，保留预算。", usage=usage) from None
            except httpx.TimeoutException:
                raise SpeechError("SPEECH_TIMEOUT", "语音响应超时，费用未知，保留预算。", usage=usage) from None
            except httpx.HTTPError:
                raise SpeechError("SPEECH_REQUEST_FAILED", "语音请求失败，费用未知，保留预算。", usage=usage) from None
            if response.status_code != 200:
                raise SpeechError("SPEECH_HTTP_ERROR", "语音服务返回错误，保留预算待核对。", usage=usage)
            try:
                envelope = response.json()
                if not isinstance(envelope, dict) or envelope.get("status_code", 200) != 200 or envelope.get("code"):
                    raise ValueError("invalid response")
                provider_usage = envelope.get("usage", {})
                units = provider_usage.get(unit_name) if isinstance(provider_usage, dict) else None
                valid = type(units) is int and 0 <= units <= max_units
                request_id = envelope.get("request_id")
                request_id = request_id if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id) else None
                usage = _usage(model, cost=units * price / 1e6 if valid and not uncertain else None,
                               status="known" if valid and not uncertain else "unknown", request_id=request_id, retries=attempt,
                               seconds=units if valid and unit_name == "duration" else None,
                               characters=units if valid and unit_name == "characters" else None)
                return envelope, usage
            except (ValueError, TypeError):
                raise SpeechError("INVALID_SPEECH_RESULT", "语音响应结构无效，保留预算待核对。", usage=usage) from None
        raise SpeechError("SPEECH_REQUEST_FAILED", "未获得有效语音结果。", usage=usage)

    async def transcribe(self, chunks: list[AudioChunk], *, reservation=None) -> dict:
        self._guard()
        prepared = _chunk_data(chunks)
        unsent = _usage(ASR_MODEL, cost=0, status="not_sent")
        await self._budget(reservation, unsent)
        client, records, result = self._client(unsent), [], []
        current = unsent
        try:
            async with client:
                for chunk, data in prepared:
                    current = _usage(ASR_MODEL)
                    body = {"model": ASR_MODEL, "input": {"messages": [{"role": "user", "content": [{
                        "type": "input_audio", "input_audio": {"data": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")}}]}]},
                        "parameters": {"format": "wav", "sample_rate": "16000"}}
                    envelope, current = await self._post(client, body, reservation, ASR_MODEL, "duration",
                                                         math.ceil(chunk.duration), ASR_MICROYUAN_PER_SECOND)
                    result.extend(parse_transcription(envelope, chunk))
                    records.append(current)
                    current = _usage(ASR_MODEL, cost=0, status="not_sent")
            return {"result": result, "usage": _combine(records, ASR_MODEL)}
        except SpeechError as exc:
            exc.usage = _combine([*records, exc.usage or current], ASR_MODEL)
            raise
        except Exception:
            raise SpeechError("SPEECH_CLIENT_FAILED", "语音处理未完成，请核对预算。",
                              usage=_combine([*records, current], ASR_MODEL)) from None

    async def _audio_bytes(self, client, audio: dict) -> bytes:
        if not isinstance(audio, dict):
            raise SpeechError("INVALID_TTS_RESULT", "配音响应缺少音频。")
        encoded = audio.get("data")
        if encoded:
            try:
                if not isinstance(encoded, str) or len(encoded) > MAX_AUDIO_BYTES * 4 // 3 + 4:
                    raise ValueError("size")
                data = base64.b64decode(encoded, validate=True)
                if not data or len(data) > MAX_AUDIO_BYTES:
                    raise ValueError("size")
                if data.startswith(b"RIFF"):
                    return data
                # Qwen3-TTS Base64 PCM uses mono signed 16-bit little-endian, 24 kHz.
                if len(data) % 2:
                    raise ValueError("unaligned PCM")
                stream = io.BytesIO()
                with wave.open(stream, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(data)
                return stream.getvalue()
            except (ValueError, TypeError, wave.Error):
                raise SpeechError("INVALID_TTS_RESULT", "配音Base64或PCM数据无效。") from None
        url = audio.get("url")
        try:
            if not isinstance(url, str) or len(url) > 8192:
                raise ValueError("invalid URL")
            parts = urlsplit(url)
            if (parts.scheme not in ("https", "http") or parts.hostname != TTS_AUDIO_HOST
                    or parts.username or parts.password or parts.port is not None or parts.fragment
                    or not parts.path.startswith("/") or "\\" in url or any(char.isspace() for char in url)):
                raise ValueError("unsafe URL")
            # The vendor example uses HTTP. Upgrade this single known OSS host;
            # never download cleartext or fall back if HTTPS is unavailable.
            secure_url = urlunsplit(("https", TTS_AUDIO_HOST, parts.path, parts.query, ""))
        except ValueError:
            raise SpeechError("AUDIO_URL_NOT_ALLOWED", "配音下载地址不在北京音频白名单中。") from None
        data = bytearray()
        try:
            async with client.stream("GET", secure_url, follow_redirects=False, headers={"Accept": "audio/wav"}) as response:
                if response.status_code != 200:
                    raise SpeechError("AUDIO_DOWNLOAD_FAILED", "配音下载失败，不跟随跳转地址。")
                async for block in response.aiter_bytes():
                    data.extend(block)
                    if len(data) > MAX_AUDIO_BYTES:
                        raise SpeechError("INVALID_TTS_RESULT", "配音文件超出本期大小限制。")
        except httpx.HTTPError:
            raise SpeechError("AUDIO_DOWNLOAD_FAILED", "配音下载未完成，已发生的合成费用仍需保留。") from None
        return bytes(data)

    async def synthesize(self, text: str, output_path: Path, *, reservation=None) -> dict:
        self._guard()
        characters = billable_characters(text)
        output_path = _local_path(output_path, existing=False)
        if output_path.exists() or not output_path.parent.is_dir() or output_path.suffix.lower() != ".wav":
            raise SpeechError("INVALID_AUDIO_PATH", "配音需要受控目录中尚不存在的WAV输出路径。")
        current = _usage(TTS_MODEL, cost=0, status="not_sent")
        await self._budget(reservation, current)
        client = self._client(current)
        try:
            async with client:
                current = _usage(TTS_MODEL)
                body = {"model": TTS_MODEL, "input": {"text": text, "voice": "Cherry", "language_type": "Chinese"}}
                envelope, current = await self._post(client, body, reservation, TTS_MODEL, "characters",
                                                     characters, TTS_MICROYUAN_PER_CHARACTER)
                output = envelope["output"]
                if not isinstance(output, dict) or output.get("finish_reason") != "stop":
                    raise SpeechError("INVALID_TTS_RESULT", "配音未正常完成。")
                data = await self._audio_bytes(client, output.get("audio"))
                duration = _wav_info(data, asr=False)
                # The pipeline registers this UUID path before calling us and
                # publishes it only after this method returns. Write exclusively
                # there so crash recovery can identify even a partially written WAV.
                created = False
                try:
                    with output_path.open("xb") as handle:
                        created = True
                        handle.write(data)
                        handle.flush()
                        os.fsync(handle.fileno())
                except Exception:
                    if created:
                        output_path.unlink(missing_ok=True)
                    raise
            return {"result": {"path": str(output_path), "duration": duration}, "usage": current}
        except SpeechError as exc:
            if exc.usage is None:
                exc.usage = current
            raise
        except Exception:
            raise SpeechError("TTS_PROCESSING_FAILED", "配音未完成，原文件未覆盖，请核对费用。", usage=current) from None
