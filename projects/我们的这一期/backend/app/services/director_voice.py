"""Short owner recordings, with per-recording consent and durable ASR jobs."""
import hashlib
import json
import math
import re
import wave

from pydantic import BaseModel
from typing import Literal

from ..models import Episode, Job
from ..repository import DomainError
from . import media
from .artifacts import artifact_workspace
from .minimax_asr import MiniMaxASR, MODEL, asr_estimate
from .speech import AudioChunk, SpeechError

MAX_SECONDS = 60
MAX_BYTES = 12 * 1024 * 1024
CLIENT_ID = re.compile(r'[A-Za-z0-9_-]{8,80}')
VOICE_KINDS = ('director_voice_upload', 'director_voice')


class VoiceError(BaseModel):
    code: str
    message: str


class VoiceOut(BaseModel):
    id: str
    job_id: str
    state: Literal['queued', 'running', 'succeeded', 'failed']
    text: str | None
    error: VoiceError | None
    mode: Literal['cloud', 'demo']


class VoicesOut(BaseModel):
    items: list[VoiceOut]


class VoiceStatus(BaseModel):
    can_record: bool
    reason: str
    provider: Literal['MiniMax'] = 'MiniMax'
    model: Literal['asr-1.0'] = 'asr-1.0'
    mode: Literal['cloud', 'demo']
    max_seconds: Literal[60] = 60
    max_bytes: Literal[12582912] = 12582912


def out(job):
    return VoiceOut(id=job.id, job_id=job.id, state=job.state,
                    text=(job.result or {}).get('text') if job.state == 'succeeded' else None,
                    error=job.error, mode=job.payload.get('mode', 'cloud'))


def ready(services):
    try:
        if not hasattr(services, 'voice_ready'):
            return False, '当前服务尚未接入导演语音。'
        return services.voice_ready()
    except Exception:
        return False, '导演语音暂不可用，请检查服务端配置。'


def require_ready(services):
    allowed, reason = ready(services)
    if not allowed:
        raise DomainError('DIRECTOR_VOICE_UNAVAILABLE', reason, 409)


def provider_guard(config):
    key = config.get('MINIMAX_API_KEY')
    if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
        raise SpeechError('KEY_NOT_CONFIGURED', 'MiniMax服务端密钥未配置。')
    if config.get('MINIMAX_ASR_MODEL', MODEL) != MODEL:
        raise SpeechError('MODEL_NOT_ALLOWED', '导演语音仅适配当前MiniMax asr-1.0。')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def normalise(source, target, suffix):
    """Decode all audio locally; streaming WebM may legitimately omit duration."""
    demuxers = {'.wav': 'wav', '.webm': 'matroska', '.mp4': 'mov', '.m4a': 'mov'}
    data = media._run('ffprobe', ['-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-f', demuxers[suffix], '-show_format', '-show_streams', '-of', 'json', str(source)],
        timeout=15, code='VOICE_INVALID_FILE')
    try:
        info = json.loads(data)
        streams = info['streams']
        if (not isinstance(streams, list) or not streams or len(streams) > 8
                or any(s.get('codec_type') != 'audio' for s in streams)
                or len(streams) != 1):
            raise ValueError('not one pure audio track')
        value = info.get('format', {}).get('duration')
        if value is not None and (not math.isfinite(float(value)) or not 0 < float(value) <= MAX_SECONDS + .1):
            raise DomainError('VOICE_DURATION_EXCEEDED', '每段语音最多60秒。', 422)
    except (ValueError, KeyError, TypeError):
        raise DomainError('VOICE_INVALID_FILE', '录音必须是真实的单音轨音频，不能包含视频。', 422) from None
    media._run('ffmpeg', ['-nostdin', '-v', 'error', '-n', '-protocol_whitelist', 'file,pipe',
        '-f', demuxers[suffix], '-i', str(source), '-map', '0:a:0', '-vn', '-sn', '-dn',
        '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-threads', '2',
        '-t', '61', str(target)], timeout=40, code='VOICE_DECODE_FAILED')
    try:
        with wave.open(str(target), 'rb') as audio:
            frames, rate = audio.getnframes(), audio.getframerate()
            duration = frames / rate
            if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or rate != 16000 or not 0 < duration <= MAX_SECONDS:
                raise ValueError('duration or encoding')
            if len(audio.readframes(frames)) != frames * 2:
                raise ValueError('incomplete')
        return duration
    except (wave.Error, EOFError, ValueError, ZeroDivisionError):
        raise DomainError('VOICE_DURATION_EXCEEDED', '录音为空、损坏或超过60秒，请重新录制。', 422) from None


def validate_job(repo, db, job):
    if not job or job.kind != 'director_voice':
        raise DomainError('VOICE_UNAVAILABLE', '语音任务不可用。', 409)
    episode = repo.owned(db, Episode, job.episode_id, job.owner_id)
    consent = job.payload.get('voice_consent', {})
    if (not isinstance(consent, dict) or consent.get('confirm_send') is not True
            or consent.get('accept_unknown_retention') is not True
            or consent.get('provider') != 'minimax' or consent.get('model') != MODEL
            or consent.get('owner_id') != job.owner_id or consent.get('episode_id') != job.episode_id
            or consent.get('client_id') != job.payload.get('client_id')
            or consent.get('voice_sha256') != job.payload.get('voice_sha256')):
        raise DomainError('VOICE_CONSENT_REQUIRED', '这段录音尚未获准发送到MiniMax转写。', 409)
    relative = job.payload.get('voice_path')
    if not isinstance(relative, str) or not re.fullmatch(r'outputs/[a-f0-9]{32}\.wav', relative):
        raise DomainError('VOICE_SOURCE_CHANGED', '应用内录音副本不可用。', 409)
    path = repo.data_dir / relative
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file() or digest(path) != job.payload.get('voice_sha256'):
        raise DomainError('VOICE_SOURCE_CHANGED', '应用内录音已变化，未发送。', 409)
    return episode, []


class ConsentedMiniMaxASR(MiniMaxASR):
    """Only this persisted recording's consent replaces global cloud admission."""
    def __init__(self, config, repo, job_id, permit):
        super().__init__(config, budget_check=permit)
        self.repo, self.job_id = repo, job_id
        self._voice_chunks = []

    async def transcribe(self, chunks, *, reservation=None):
        self._voice_chunks = list(chunks)
        return await super().transcribe(self._voice_chunks, reservation=reservation)

    def _guard(self):
        provider_guard(self._config)
        with self.repo.factory() as db:
            job = db.get(Job, self.job_id)
            validate_job(self.repo, db, job)
            source = self.repo.data_dir / job.payload['voice_path']
        # Bind every outbound part to the exact consented PCM; another local
        # audio path cannot borrow this recording's budget/permission.
        from .speech import _chunk_data
        try:
            with wave.open(str(source), 'rb') as original:
                position = 0
                for chunk, _ in _chunk_data(self._voice_chunks):
                    with wave.open(str(chunk.path), 'rb') as audio:
                        frames = audio.getnframes()
                        if (chunk.asset_id != self.job_id or chunk.offset != position / 16000
                                or audio.readframes(frames) != original.readframes(frames)):
                            raise ValueError('unbound audio')
                        position += frames
                if not position or original.readframes(1):
                    raise ValueError('incomplete audio')
        except (OSError, EOFError, wave.Error, ValueError):
            raise SpeechError('VOICE_SOURCE_CHANGED', '发送内容与这次授权录音不一致，未发送。') from None


def chunks_for(path, directory, job_id):
    result = []
    with wave.open(str(path), 'rb') as source:
        position = 0
        while data := source.readframes(30 * 16000):
            target = directory / f'voice-{position}.wav'
            with wave.open(str(target), 'wb') as part:
                part.setnchannels(1); part.setsampwidth(2); part.setframerate(16000); part.writeframes(data)
            frames = len(data) // 2
            result.append(AudioChunk(target, job_id, position / 16000, frames / 16000))
            position += frames
    return result


def run(pipeline, job_id):
    repo = pipeline.repo
    with repo.factory() as db:
        job = db.get(Job, job_id)
        validate_job(repo, db, job)
        source = repo.data_dir / job.payload['voice_path']
        source_digest = job.payload['voice_sha256']
    pipeline.stage(job_id, '正在转成文字，请稍候核对')
    with artifact_workspace(repo, job_id) as directory:
        chunks = chunks_for(source, directory, job_id)
        result = pipeline.invoke(job_id, 'director_voice', asr_estimate(chunks),
            lambda reservation, permit: pipeline.services.director_voice(chunks, reservation, permit,
                {'job_id': job_id}), request_digest=source_digest, checkpoint_key='director_voice')
    if not isinstance(result, list) or any(not isinstance(row, dict) or not isinstance(row.get('text'), str) for row in result):
        raise DomainError('INVALID_VOICE_RESULT', '转写结果格式无效，请核对费用后重试。', 409)
    text = ''.join(row['text'] for row in result).strip()
    if not text:
        raise DomainError('VOICE_NO_SPEECH', '没有识别到清楚的话，请重新录制或改用文字。', 422)
    if len(text) > 2000:
        raise DomainError('VOICE_TEXT_TOO_LONG', '识别文字超过本轮对话上限，请缩短录音。', 422)
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        validate_job(repo, db, job)
        pipeline.finish(db, job, {'text': text}, '文字已就绪，核对后再发给导演')
