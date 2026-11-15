#!/usr/bin/env python3
"""One approved fictional narration -> real ASR, without production admission.

No arbitrary media/text/endpoint options. A separate ASR validation task shares
the normal global ledger, including the unresolved earlier TTS reservation.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from sqlalchemy import select
from app.models import Episode, Job, ModelCall, Budget
from app.repository import Repository, DomainError
from app.retention_models import Retention
from app.services.artifacts import artifact_path, artifact_workspace
from app.services.minimax_asr import MiniMaxASR, MODEL, asr_estimate
from app.services.pipeline import Pipeline
from app.services.speech import SpeechError, AudioChunk, _chunk_data
from cloud_preflight import read_configuration

SOURCE = 'outputs/46a3eee5129c434bb8ee60d618ea7d64.wav'
SOURCE_SHA = 'ccb001bfddb11a9598335dd6eea6a0e6e8896485adfd5632794202d738fd90b9'
PREPARED_SHA = '0f1d546f2fa6e4bb9a5829998b0b8f50b0843a4ab828d679d57e6a66873fefc3'
EXPECTED_TEXT = '说好今晚不点外卖。结果，锅还没热，菜单先打开了。谁先破功？在场，开拍！'
SCOPE = 'synthetic-minimax-asr-v1-approved-20261112'
OWNER = 'synthetic-minimax-asr-technical-test'
EPISODE_ID = hashlib.sha256(OWNER.encode()).hexdigest()[:32]


class FixedASR(MiniMaxASR):
    def __init__(self, key, *, authorized=False, **kwargs):
        self.authorized = authorized
        super().__init__({'MINIMAX_API_KEY': key, 'MINIMAX_ASR_MODEL': MODEL}, **kwargs)

    def _guard(self):
        if self.authorized is not True:
            raise SpeechError('SYNTHETIC_NOT_AUTHORIZED', '未授权固定虚构音频测试。')
        key = self._config.get('MINIMAX_API_KEY')
        if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
            raise SpeechError('KEY_NOT_CONFIGURED', '请先在本机.env配置MiniMax密钥。')
        if self._config.get('MINIMAX_ASR_MODEL') != MODEL:
            raise SpeechError('SYNTHETIC_INPUT_REJECTED', '本次仅允许固定转写型号。')

    async def transcribe(self, chunks, *, reservation=None):
        self._guard()
        prepared = _chunk_data(chunks)
        if (len(prepared) != 1 or prepared[0][0].asset_id != 'fictional-narration'
                or prepared[0][0].offset != 0 or hashlib.sha256(prepared[0][1]).hexdigest() != PREPARED_SHA):
            raise SpeechError('SYNTHETIC_INPUT_REJECTED', '仅允许已确认旁白的固定音频，未发送其他素材。')
        return await super().transcribe(chunks, reservation=reservation)


class Services:
    is_real = True

    def __init__(self, key, authorized):
        self.key, self.authorized = key, authorized

    def ready(self):
        return FixedASR(self.key, authorized=self.authorized).ready()

    def profile(self):
        return {'scope': SCOPE, 'provider': 'minimax', 'model': MODEL,
                'source_sha256': SOURCE_SHA, 'prepared_sha256': PREPARED_SHA,
                'sample_type': 'fictional-only', 'retention_verified': False,
                'response_format': 'verbose_json', 'timestamp_level': 'word'}


def prepare(repo, directory):
    source = artifact_path(repo, SOURCE)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1_500_000:
        raise DomainError('SOURCE_MISSING', '固定旁白文件缺失或异常，不发送其他素材。')
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != SOURCE_SHA:
        raise DomainError('SOURCE_CHANGED', '固定旁白指纹不符，不发送音频。')
    # Resample the verified bytes, never reopen the original after validation.
    original, target = directory / 'source.wav', directory / 'asr.wav'
    with original.open('xb') as f:
        f.write(data)
    completed = subprocess.run([shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg',
        '-nostdin', '-v', 'error', '-n', '-i', str(original), '-ac', '1', '-ar', '16000',
        '-c:a', 'pcm_s16le', '-map_metadata', '-1', str(target)], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if completed.returncode or hashlib.sha256(target.read_bytes()).hexdigest() != PREPARED_SHA:
        raise DomainError('PREPARATION_CHANGED', '固定音频转换结果与批准指纹不同，停止调用。')
    with wave.open(str(target), 'rb') as wav:
        seconds = wav.getnframes() / wav.getframerate()
    chunks = [AudioChunk(target, 'fictional-narration', 0, seconds)]
    _chunk_data(chunks)
    return chunks


def ensure_job(repo, profile):
    with repo.transaction() as db:
        ep = db.get(Episode, EPISODE_ID)
        if ep is None:
            ep = Episode(id=EPISODE_ID, owner_id=OWNER, title='MiniMax固定虚构音频转写技术测试')
            db.add(ep); db.flush()
            db.add(Retention(episode_id=ep.id, owner_id=OWNER, expires_at=time.time() + 30 * 86400))
        repo.owned(db, Episode, ep.id, OWNER)
        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE)).first()
        if job:
            if job.payload.get('model_profile') != profile:
                raise DomainError('PROBE_CHANGED', '固定样例或参数已变化，不重复调用。')
            return job.id
        job = Job(owner_id=OWNER, episode_id=ep.id, kind='synthetic_minimax_asr', state='running',
                  idempotency_key=SCOPE, payload={'asset_ids': [], 'revision': 0, 'model_profile': profile})
        db.add(job); db.flush()
        repo.audit(db, OWNER, 'synthetic_test_authorized', job.id, scope=SCOPE,
                   approval_date='2026-11-12', retention_verified=False)
        return job.id


def report(repo, jid):
    with repo.factory() as db:
        job = repo.owned(db, Job, jid, OWNER)
        calls = []
        for call in db.scalars(select(ModelCall).where(ModelCall.job_id == jid).order_by(ModelCall.created_at)):
            ledger = db.get(Budget, call.reservation_id)
            calls.append({'time': call.created_at, 'state': call.state, 'usage': call.usage,
                          'elapsed_seconds': call.elapsed, 'ledger_state': ledger.state,
                          'settled_yuan': ledger.cost / 1e6 if ledger.cost is not None else None})
        words = job.payload.get('steps', {}).get('asr', [])
        recognized = ''.join(row['text'] for row in words)
        comparable = lambda s: ''.join(c for c in s if not unicodedata.category(c).startswith(('P', 'Z')))
        result = {'scope': SCOPE, 'sample_type': 'fictional-only', 'job_id': jid,
                  'state': job.state, 'error': job.error, 'model_profile': job.payload['model_profile'],
                  'words': words, 'recognized_text': recognized,
                  'characters_match_excluding_punctuation': comparable(recognized) == comparable(EXPECTED_TEXT),
                  'timestamp_units': len(words),
                  'expected_text': EXPECTED_TEXT, 'calls': calls, 'budget': repo.budget(db, EPISODE_ID),
                  'content_acceptance': '单人合成旁白技术验证，不代表多人聚会、噪声或完整出片验收'}
    directory = repo.data_dir / '技术测试/MiniMax固定转写'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=directory, encoding='utf-8', delete=False) as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        temporary = Path(f.name)
    os.replace(temporary, directory / '技术调用记录.json')
    return result


def run_probe(repo, key, *, authorized=False):
    services = Services(key, authorized)
    pipeline = Pipeline(repo, services)
    pipeline.require_ready()
    jid = ensure_job(repo, services.profile())
    with repo.transaction() as db:
        job = repo.owned(db, Job, jid, OWNER)
        if job.state == 'succeeded':
            return report(repo, jid)
        if 'asr' not in job.payload.get('steps', {}) and db.scalars(select(ModelCall).where(ModelCall.job_id == jid)).first():
            raise DomainError('PROBE_NEEDS_REVIEW', '已有转写调用未成功，先核对原因和费用，不自动重发。')
        job.state, job.error = 'running', None
    try:
        with artifact_workspace(repo, jid) as directory:
            chunks = prepare(repo, directory)
            pipeline.invoke(jid, 'asr', asr_estimate(chunks), lambda reservation, permit:
                FixedASR(key, authorized=authorized, budget_check=permit).transcribe(chunks, reservation=reservation),
                checkpoint_key='asr')
        with repo.transaction() as db:
            job = repo.owned(db, Job, jid, OWNER)
            job.state, job.stage = 'succeeded', '固定音频转写接口与时间戳检查通过'
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '固定音频转写未完成'
            job.error = {'code': exc.code if isinstance(exc, (DomainError, SpeechError)) else 'PROBE_FAILED',
                         'message': exc.message if isinstance(exc, (DomainError, SpeechError)) else '本机准备或测试未完成。'}
        report(repo, jid)
        raise
    return report(repo, jid)


@contextmanager
def probe_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.synthetic-minimax-asr.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError('PROBE_RUNNING', '已有转写测试运行中。') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'run', 'report'))
    args = parser.parse_args(argv)
    try:
        values = read_configuration(PRODUCT / 'backend/.env')
        root = Path(values.get('APP_DATA_DIR') or PRODUCT / 'data').resolve()
        if 'demo' in root.name.lower() or root.is_relative_to((PRODUCT / 'data/demo').resolve()):
            raise DomainError('DEMO_NOT_ALLOWED', '不能使用演示目录进行真实测试。')
        key = values.get('MINIMAX_API_KEY', '')
        if args.action == 'check':
            ready, message = Services(key, True).ready()
            if not ready:
                raise DomainError('CONFIG_NOT_READY', message)
            print('固定虚构音频转写配置就绪；未调用接口，未开启正式云处理。')
            return 0
        with probe_lock(root):
            repo = Repository(root)
            try:
                if args.action == 'run':
                    result = run_probe(repo, key, authorized=True)
                else:
                    with repo.factory() as db:
                        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE)).first()
                        if job is None:
                            raise DomainError('NO_RECORD', '尚无真实转写测试记录。')
                        jid = job.id
                    result = report(repo, jid)
                print(json.dumps({k: result[k] for k in ('state', 'recognized_text', 'budget', 'calls')}, ensure_ascii=False))
                return 0 if result['state'] == 'succeeded' else 2
            finally:
                repo.engine.dispose()
    except (DomainError, SpeechError) as exc:
        print(exc.code + '：' + exc.message)
        return 2
    except (Exception, KeyboardInterrupt):
        print('转写测试中断；如已发送，费用预留继续占用，未自动重试。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
