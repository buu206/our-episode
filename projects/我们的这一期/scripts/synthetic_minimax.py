#!/usr/bin/env python3
"""One fixed fictional TTS audition; never imported by the production server.

No arbitrary text/media/endpoint arguments; no production admission flags changed.
Uses the normal persistent budget and checkpoints, including previous provider spend.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import httpx

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from dotenv import dotenv_values
from sqlalchemy import select
from app.models import Episode, Job, ModelCall, Budget
from app.repository import Repository, DomainError
from app.retention_models import Retention
from app.services.artifacts import record_artifact, artifact_path
from app.services.minimax_speech import MiniMaxSpeech, MODEL, tts_estimate
from app.services.pipeline import Pipeline
from app.services.speech import SpeechError

TEXT = '说好今晚不点外卖。结果，锅还没热，菜单先打开了。谁先破功？在场，开拍！'
SCOPE = 'synthetic-minimax-v1-approved-20261112'
OWNER = 'synthetic-minimax-technical-test'
EPISODE_ID = hashlib.sha256(OWNER.encode()).hexdigest()[:32]
VOICE_CONFIG = {'MINIMAX_TTS_MODEL': MODEL, 'MINIMAX_TTS_VOICE': 'male-qn-daxuesheng',
                'MINIMAX_TTS_SPEED': '1.15', 'MINIMAX_TTS_EMOTION': 'happy', 'MINIMAX_TTS_PITCH': '0'}


class FixedSpeech(MiniMaxSpeech):
    def __init__(self, key, *, authorized=False, **kwargs):
        self.authorized = authorized
        super().__init__({**VOICE_CONFIG, 'MINIMAX_API_KEY': key}, **kwargs)

    def _guard(self):
        if self.authorized is not True:
            raise SpeechError('SYNTHETIC_NOT_AUTHORIZED', '未授权固定虚构短句测试。')
        key = self._config.get('MINIMAX_API_KEY')
        if (not isinstance(key, str) or not key or not key.isascii()
                or any(c.isspace() for c in key)):
            raise SpeechError('KEY_NOT_CONFIGURED', '请先在本机.env配置MiniMax密钥。')
        if any(self._config.get(k) != v for k, v in VOICE_CONFIG.items()):
            raise SpeechError('SYNTHETIC_INPUT_REJECTED', '本次只允许固定系统音色与试听参数。')
        self._voice()

    async def synthesize(self, text, output_path, *, reservation=None):
        self._guard()
        if text != TEXT:
            raise SpeechError('SYNTHETIC_INPUT_REJECTED', '测试入口只允许固定虚构短句。')
        return await super().synthesize(text, output_path, reservation=reservation)


class Services:
    is_real = True

    def __init__(self, key, authorized):
        self.key, self.authorized = key, authorized

    def ready(self):
        return FixedSpeech(self.key, authorized=self.authorized).ready()

    def profile(self):
        return {'scope': SCOPE, 'provider': 'minimax', 'model': MODEL,
                'voice': VOICE_CONFIG, 'text_sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
                'sample_type': 'fictional-only', 'retention_verified': False}


class ReviewedFixedProbe(Pipeline):
    """Operator-only second attempt, retaining the FULL unknown first reservation.

Production Pipeline is unchanged: it still stops on every unknown cost. This
script-only subclass accepts exactly one failed fixed-sample TTS after a fresh
successful read-only authentication check. Repository.reserve counts BOTH
reservations against the same episode/global limits; no costs are forgiven.
"""
    def __init__(self, repo, services, reviewed_call_id=None):
        super().__init__(repo, services)
        self.reviewed_call_id = reviewed_call_id

    def _check_pending_costs(self, db, job):
        if self.reviewed_call_id is None:
            return super()._check_pending_costs(db, job)
        calls = list(db.scalars(select(ModelCall).where(ModelCall.episode_id == EPISODE_ID)))
        ledgers = list(db.scalars(select(Budget).where(Budget.episode_id == EPISODE_ID)))
        if (job.owner_id != OWNER or job.episode_id != EPISODE_ID or job.kind != 'synthetic_minimax'
                or len(calls) != 1 or len(ledgers) != 1):
            raise DomainError('REVIEW_MISMATCH', '固定试听补试范围不匹配，停止发送。')
        call, ledger = calls[0], ledgers[0]
        if (call.id != self.reviewed_call_id or call.job_id != job.id or call.owner_id != OWNER
                or call.state != 'failed' or call.kind != 'tts' or call.reservation_id != ledger.id
                or ledger.owner_id != OWNER or ledger.state != 'unknown' or ledger.cost is not None
                or ledger.amount != round(tts_estimate(TEXT) * 1e6)):
            raise DomainError('REVIEW_MISMATCH', '旧调用与保留的预算不匹配，停止发送。')
        self.repo.audit(db, OWNER, 'synthetic_retry_reviewed', call.id,
                        reason='replacement_key_authenticated', retained_unknown_microyuan=ledger.amount,
                        max_total_synthesis_attempts=2)


def authenticate(key):
    """Read-only system voice listing. Never returns provider text or credentials."""
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
            response = client.post('https://api.minimax.cn/v1/get_voice',
                headers={'Authorization': 'Bearer ' + key}, json={'voice_type': 'system'})
            value = response.json()
            code = value.get('base_resp', {}).get('status_code')
            voices = value.get('system_voice')
            if (response.status_code == 200 and type(code) is int and code == 0
                    and isinstance(voices, list) and any(isinstance(v, dict)
                    and v.get('voice_id') == VOICE_CONFIG['MINIMAX_TTS_VOICE'] for v in voices)):
                return
    except Exception:
        pass
    raise DomainError('AUTH_CHECK_FAILED', '新密钥只读鉴权或候选音色查询未通过，不补发语音请求。')


def configuration():
    values = {**dotenv_values(PRODUCT / 'backend/.env'), **os.environ}
    root = Path(values.get('APP_DATA_DIR') or PRODUCT / 'data').resolve()
    if 'demo' in root.name.lower():
        raise DomainError('DEMO_NOT_ALLOWED', '不能使用演示目录进行真实测试。')
    return root, values.get('MINIMAX_API_KEY', '')


@contextmanager
def probe_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.synthetic-minimax.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError('PROBE_RUNNING', '已有旁白测试运行中。') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def ensure_job(repo, profile):
    with repo.transaction() as db:
        ep = db.get(Episode, EPISODE_ID)
        if ep is None:
            ep = Episode(id=EPISODE_ID, owner_id=OWNER, title='MiniMax虚构旁白技术试听')
            db.add(ep); db.flush()
            db.add(Retention(episode_id=ep.id, owner_id=OWNER, expires_at=time.time() + 30 * 86400))
        repo.owned(db, Episode, ep.id, OWNER)
        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE)).first()
        if job:
            if job.payload.get('model_profile') != profile:
                raise DomainError('PROBE_CHANGED', '固定短句或参数已变化，保留旧记录，不重复调用。')
            return job.id
        job = Job(owner_id=OWNER, episode_id=ep.id, kind='synthetic_minimax', state='running',
                  idempotency_key=SCOPE, payload={'asset_ids': [], 'revision': 0, 'model_profile': profile})
        db.add(job); db.flush()
        repo.audit(db, OWNER, 'synthetic_test_authorized', job.id, scope=SCOPE,
                   approval_date='2026-11-12', retention_verified=False)
        return job.id


def report(repo, jid):
    with repo.factory() as db:
        job = repo.owned(db, Job, jid, OWNER)
        rows = []
        for call in db.scalars(select(ModelCall).where(ModelCall.job_id == jid).order_by(ModelCall.created_at)):
            ledger = db.get(Budget, call.reservation_id)
            rows.append({'time': call.created_at, 'state': call.state, 'usage': call.usage,
                         'elapsed_seconds': call.elapsed, 'ledger_state': ledger.state,
                         'settled_yuan': ledger.cost / 1e6 if ledger.cost is not None else None})
        result = {'scope': SCOPE, 'sample_type': 'fictional-only', 'job_id': jid,
                  'state': job.state, 'error': job.error, 'model_profile': job.payload['model_profile'],
                  'audio': job.payload.get('steps', {}).get('tts'),
                  'calls': rows, 'budget': repo.budget(db, EPISODE_ID),
                  'content_acceptance': '音色由用户试听；转写与完整产品链路仍待验'}
    directory = repo.data_dir / '技术测试/MiniMax固定旁白'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=directory, encoding='utf-8', delete=False) as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        temporary = Path(f.name)
    os.replace(temporary, directory / '技术调用记录.json')
    return result


def run_probe(repo, key, *, authorized=False, retry_after_key_fix=False):
    services = Services(key, authorized)
    pipeline = ReviewedFixedProbe(repo, services)
    pipeline.require_ready()
    jid = ensure_job(repo, services.profile())
    with repo.transaction() as db:
        job = repo.owned(db, Job, jid, OWNER)
        if job.state == 'succeeded':
            return report(repo, jid)
        calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == jid)))
        review = None
        if calls and 'tts' not in job.payload.get('steps', {}):
            if (retry_after_key_fix is not True or len(calls) != 1
                    or (job.error or {}).get('code') not in ('TTS_PROVIDER_ERROR', 'TTS_AUTH_FAILED')):
                raise DomainError('PROBE_NEEDS_REVIEW', '已有调用未成功，先核对费用和原因，不自动重发。')
            review = calls[0].id
    if review:
        authenticate(key)
        pipeline.reviewed_call_id = review
    with repo.transaction() as db:
        job = repo.owned(db, Job, jid, OWNER)
        job.state, job.error = 'running', None
    relative = f'outputs/{jid}.wav'
    record_artifact(repo, jid, relative)
    try:
        pipeline.invoke(jid, 'tts', tts_estimate(TEXT), lambda reservation, permit:
            FixedSpeech(key, authorized=authorized, budget_check=permit).synthesize(
                TEXT, artifact_path(repo, relative), reservation=reservation), checkpoint_key='tts')
        with repo.transaction() as db:
            job = repo.owned(db, Job, jid, OWNER)
            job.state, job.stage = 'succeeded', '真实旁白接口与音频检查通过，等待用户试听'
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '旁白技术测试未完成'
            job.error = {'code': exc.code if isinstance(exc, DomainError) else 'PROBE_FAILED',
                         'message': exc.message if isinstance(exc, DomainError) else '测试未完成，请核对记录。'}
        report(repo, jid)
        raise
    return report(repo, jid)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'run', 'report'))
    parser.add_argument('--retry-after-key-fix', action='store_true',
        help='Operator-reviewed one-time fixed-sample retry, retaining all old unknown costs.')
    args = parser.parse_args(argv)
    try:
        root, key = configuration()
        if args.action in ('check', 'run'):
            ready, message = Services(key, True).ready()
            if not ready:
                raise DomainError('CONFIG_NOT_READY', message)
        if args.action == 'check':
            print(json.dumps({'key_configured': True, 'sample_text': TEXT,
                  'voice': VOICE_CONFIG, 'reservation_yuan': tts_estimate(TEXT),
                  'network_requests': 0}, ensure_ascii=False))
            return 0
        with probe_lock(root):
            repo = Repository(root)
            try:
                if args.action == 'run':
                    result = run_probe(repo, key, authorized=True, retry_after_key_fix=args.retry_after_key_fix)
                else:
                    with repo.factory() as db:
                        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE)).first()
                        if job is None:
                            raise DomainError('NO_RECORD', '尚无真实旁白测试记录。')
                        jid = job.id
                    result = report(repo, jid)
                print(json.dumps({k: result[k] for k in ('state', 'audio', 'budget', 'calls')}, ensure_ascii=False))
                return 0 if result['state'] == 'succeeded' else 2
            finally:
                repo.engine.dispose()
    except (DomainError, SpeechError) as exc:
        print(exc.code + '：' + exc.message)
        return 2
    except (Exception, KeyboardInterrupt):
        print('测试中断或本机处理失败；如已发送，费用预留继续占用，未自动重试。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
