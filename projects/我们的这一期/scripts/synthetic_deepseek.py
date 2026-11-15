#!/usr/bin/env python3
"""Explicitly authorized fixed synthetic-only probe. Never imported by the server.

No arbitrary input, media path, prompt, provider or endpoint arguments. Uses the
application budget ledger without opening production media or enabling cloud.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import warnings

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from dotenv import dotenv_values
from PIL import Image, ImageDraw
from sqlalchemy import select
from app.models import Episode, Job, ModelCall, Budget
from app.repository import Repository, DomainError
from app.retention_models import Retention
from app.services import deepseek as ds
from app.services.director import DirectorError, prepare_input
from app.services.pipeline import Pipeline
from app.services.vision import FrameSample, PreparedVideo, _coverage

SCOPE = 'synthetic-deepseek-v1-approved-20261111'
OWNER = 'synthetic-deepseek-technical-test'
EPISODE_ID = hashlib.sha256(OWNER.encode()).hexdigest()[:32]
ASSET = 'a' * 32
PROMPTS = PRODUCT / 'backend/app/services/prompts'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def fixtures():
    """No user data or files used to create either request."""
    picture = Image.new('RGB', (512, 512), '#ffffff')
    draw = ImageDraw.Draw(picture)
    draw.ellipse((60, 180, 200, 320), fill='#e04040')
    draw.rectangle((310, 180, 450, 320), fill='#3066d6')
    output = io.BytesIO()
    picture.save(output, format='JPEG', quality=90)
    frame = FrameSample(ASSET, .5, ASSET + '_f001', output.getvalue())
    prepared = PreparedVideo((frame,), (_coverage(ASSET, 1.),))
    evidence = [{'asset_id': ASSET, 'start': 0., 'end': 1.,
                 'note': '虚构技术样例：整段1秒为同一静态白底画面，左侧红色圆形与右侧蓝色正方形始终同时可见；无动作、无人物、无对白。'}]
    assets = {ASSET: 1.}
    background = '完全虚构的几何图形节目：圆与方的第一次同框。仅验证输出结构，不编造真人、对白或动作。'
    return prepared, evidence, assets, background


def allowed_messages():
    prepared, evidence, assets, background = fixtures()
    frame = prepared.frames[0]
    vision = [{'role': 'user', 'content': [
        {'type': 'text', 'text': (PROMPTS / 'vision.txt').read_text()},
        {'type': 'text', 'text': json.dumps({'frame_id': frame.frame_id, 'asset_id': frame.asset_id, 'time': frame.time})},
        {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + base64.b64encode(frame.jpeg).decode(), 'detail': 'original'}},
    ]}]
    director = [{'role': 'system', 'content': (PROMPTS / 'director.txt').read_text()},
                {'role': 'user', 'content': prepare_input(evidence, assets, background)}]
    return {'vision': vision, 'directions': director}


def check_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,512}', key):
        raise DomainError('KEY_NOT_CONFIGURED', '请先在本机安全配置DeepSeek密钥；未发送请求。')


class _FixedSyntheticScope:
    """Separate script-only admission, never falsifies retention/region flags."""
    def __init__(self, key, *, kind, authorized=False, **kwargs):
        self._scope_kind = kind
        self._authorized = authorized
        super().__init__({'DEEPSEEK_API_KEY': key}, **kwargs)

    def _guard(self):
        if self._authorized is not True or self._scope_kind not in ('vision', 'directions'):
            raise DirectorError('SYNTHETIC_NOT_AUTHORIZED', '未授权固定技术样例调用。')
        try:
            check_key(self._config.get('DEEPSEEK_API_KEY'))
        except DomainError as exc:
            raise DirectorError(exc.code, exc.message) from None

    async def request(self, messages, reservation, input_limit=None):
        self._guard()
        if digest(messages) != digest(allowed_messages()[self._scope_kind]):
            raise DirectorError('SYNTHETIC_INPUT_REJECTED', '测试入口只允许固定的几何图案及虚构文本。', usage=ds.empty_usage())
        return await super().request(messages, reservation, input_limit)


class SyntheticVision(_FixedSyntheticScope, ds.DeepSeekVision):
    pass


class SyntheticDirector(_FixedSyntheticScope, ds.DeepSeekDirector):
    pass


class SyntheticServices:
    is_real = True

    def __init__(self, key, authorized=False):
        self.key, self.authorized = key, authorized

    def ready(self):
        if self.authorized is not True:
            return False, '需要显式运行已授权的固定技术样例。'
        try:
            check_key(self.key)
            return True, '仅固定非个人技术样例；供应商留存仍待核实。'
        except DomainError as exc:
            return False, exc.message

    def profile(self):
        return {'scope': SCOPE, 'provider': 'deepseek', 'model': ds.MODEL,
                'fixture_sha256': digest(allowed_messages()), 'retention_verified': False,
                'sample_type': 'synthetic-only'}


def configuration():
    # Same data-directory precedence as the normal service; no alternate --data option.
    file = dotenv_values(PRODUCT / 'backend/.env')
    values = {**file, **os.environ}
    root = Path(values.get('APP_DATA_DIR') or PRODUCT / 'data').resolve()
    if 'demo' in root.name.lower():
        raise DomainError('DEMO_NOT_ALLOWED', '不能使用演示数据目录进行真实测试。')
    return root, values.get('DEEPSEEK_API_KEY', '')


@contextmanager
def probe_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.synthetic-deepseek.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError('PROBE_RUNNING', '已有技术测试运行中，不重复发送。') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare(root):
    target = root / '技术测试/DeepSeek固定样例'
    target.mkdir(parents=True, exist_ok=True)
    prepared, evidence, assets, background = fixtures()
    manifest = {'scope': SCOPE, 'provider': ds.MODEL, 'sample_type': 'synthetic-only',
                'fixture_sha256': digest(allowed_messages()), 'evidence': evidence, 'assets': assets,
                'background': background, 'first_run_reservation_yuan': .38,
                'task_limit_yuan': 3, 'total_limit_yuan': 30, 'retention_verified': False}
    # These are review artifacts only; run always regenerates fixed input in memory.
    for name, data in [('图案.jpg', prepared.frames[0].jpeg),
                       ('测试说明.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())]:
        path = target / name
        if path.exists():
            if path.read_bytes() != data:
                raise DomainError('PREVIEW_CHANGED', '已有预览文件与当前固定样例不同，请先核对，不覆盖。')
        else:
            with path.open('xb') as handle:
                handle.write(data)
    return target


def ensure_job(repo, profile, new_revision=False):
    with repo.transaction() as db:
        ep = db.get(Episode, EPISODE_ID)
        if ep is None:
            ep = Episode(id=EPISODE_ID, owner_id=OWNER, title='固定几何样例：真实API技术测试')
            db.add(ep); db.flush()
            db.add(Retention(episode_id=ep.id, owner_id=OWNER, expires_at=time.time() + 30 * 86400))
        repo.owned(db, Episode, ep.id, OWNER)
        existing = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE)).first()
        job_key = SCOPE
        if existing:
            if existing.payload.get('model_profile') == profile:
                return existing.id
            if not new_revision:
                raise DomainError('PROBE_CHANGED', '固定样例或提示词已变化，先核对旧任务，不重复调用。')
            # Explicit revision keeps prior results and the SAME episode/global budget.
            job_key = SCOPE + ':' + profile['fixture_sha256']
            revised = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == job_key)).first()
            if revised:
                if revised.payload.get('model_profile') != profile:
                    raise DomainError('PROBE_CHANGED', '修订任务配置不一致，未发送请求。')
                return revised.id
        job = Job(owner_id=OWNER, episode_id=ep.id, kind='synthetic_deepseek', state='running',
                  idempotency_key=job_key, payload={'asset_ids': [], 'revision': 0, 'model_profile': profile})
        db.add(job); db.flush()
        repo.audit(db, OWNER, 'synthetic_test_authorized', job.id, scope=SCOPE,
                   approval_date='2026-11-11', retention_verified=False)
        return job.id


def report(repo, job_id):
    with repo.factory() as db:
        job = db.get(Job, job_id)
        calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job_id).order_by(ModelCall.created_at)))
        rows = []
        for call in calls:
            ledger = db.get(Budget, call.reservation_id)
            rows.append({'time': call.created_at, 'kind': call.kind, 'state': call.state,
                         'elapsed_seconds': call.elapsed, 'usage': call.usage,
                         'ledger_state': ledger.state, 'settled_yuan': ledger.cost / 1e6 if ledger.cost is not None else None})
        value = {'scope': SCOPE, 'sample_type': 'synthetic-only', 'job_id': job_id,
                 'state': job.state, 'error': job.error, 'budget': repo.budget(db, EPISODE_ID),
                 'calls': rows, 'content_acceptance': '真实聚会效果、语音及完整产品链路仍待验'}
    directory = repo.data_dir / '技术测试/DeepSeek固定样例'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / '技术调用记录.json'
    # Source of truth remains the transactional DB, not this rebuildable report.
    with tempfile.NamedTemporaryFile('w', dir=directory, delete=False, encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        temp = Path(handle.name)
    os.replace(temp, path)
    return value


def run_probe(repo, key, *, authorized=False, retry_failed=False, new_revision=False):
    services = SyntheticServices(key, authorized)
    pipeline = Pipeline(repo, services)
    pipeline.require_ready()
    jid = ensure_job(repo, services.profile(), new_revision)
    with repo.transaction() as db:
        job = db.get(Job, jid)
        if job.state == 'succeeded':
            return report(repo, jid)
        calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == jid)))
        for call in calls:
            ledger = db.get(Budget, call.reservation_id)
            if ledger.state != 'settled' or ledger.cost is None:
                raise DomainError('COST_UNKNOWN', '上次调用费用未核实，保留占用，不重复发送。')
        if calls and job.state != 'succeeded' and not retry_failed:
            raise DomainError('RETRY_REQUIRES_FLAG', '上次任务未完成；核对记录后才可显式重试。')
        job.state, job.error = 'running', None
    prepared, evidence, assets, background = fixtures()
    try:
        pipeline.invoke(jid, 'vision', .10, lambda reservation, permit:
            SyntheticVision(key, kind='vision', authorized=authorized, budget_check=permit).analyze(
                prepared, reservation=reservation), request_digest=prepared.request_digest)
        pipeline.invoke(jid, 'directions', .28, lambda reservation, permit:
            SyntheticDirector(key, kind='directions', authorized=authorized, budget_check=permit).propose(
                evidence, assets, background, reservation=reservation))
        with repo.transaction() as db:
            unsettled = list(db.scalars(select(Budget).where(Budget.episode_id == EPISODE_ID,
                (Budget.state != 'settled') | Budget.cost.is_(None))))
            if unsettled:
                raise DomainError('COST_UNKNOWN', '接口已返回但计费用量不完整，暂停验收。')
            job = db.get(Job, jid)
            job.state, job.stage = 'succeeded', '固定技术样例接口与结构检查完成，内容效果待评'
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '技术样例未完成'
            job.error = {'code': exc.code if isinstance(exc, DomainError) else 'PROBE_FAILED',
                         'message': exc.message if isinstance(exc, DomainError) else '测试失败，详见受控记录。'}
        report(repo, jid)
        raise
    return report(repo, jid)


def save_key(env_file, key):
    check_key(key)
    if env_file.is_symlink() or not env_file.is_file():
        raise DomainError('ENV_UNSAFE', '需要现有受控backend/.env文件。')
    result = subprocess.run(['git', 'check-ignore', '-q', str(env_file)], cwd=PRODUCT,
                            capture_output=True, timeout=10)
    if result.returncode != 0:
        raise DomainError('ENV_NOT_IGNORED', '.env未被Git忽略，不写入密钥。')
    with env_file.open('r') as source:
        fcntl.flock(source, fcntl.LOCK_EX)
        original = source.read()
        if dotenv_values(stream=io.StringIO(original)).get('DEEPSEEK_API_KEY'):
            raise DomainError('KEY_ALREADY_SET', '已存在DeepSeek密钥，本工具不覆盖。')
        updated = re.sub(r'^\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=.*$', '', original, flags=re.M)
        updated = updated.rstrip() + '\nDEEPSEEK_API_KEY=' + key + '\n'
        temp = None
        try:
            with tempfile.NamedTemporaryFile('w', prefix='.env.', dir=env_file.parent, delete=False) as handle:
                temp = Path(handle.name)
                os.chmod(temp, 0o600)
                handle.write(updated); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, env_file)
        finally:
            if temp and temp.exists():
                temp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'check', 'configure-key', 'run', 'report'))
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--new-revision', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.action == 'configure-key':
            if not sys.stdin.isatty() or not sys.stderr.isatty():
                raise DomainError('TTY_REQUIRED', '请在本机终端配置，禁止经命令参数或管道传入密钥。')
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                key = getpass.getpass('粘贴DeepSeek密钥后按回车（输入不显示）：')
            save_key(PRODUCT / 'backend/.env', key)
            print('密钥已安全保存；现有云开关、留存标记和邀请码未变，未调用模型。')
            return 0
        root, key = configuration()
        if args.action == 'prepare':
            print('固定样例已准备：' + str(prepare(root)))
            return 0
        if args.action == 'check':
            check_key(key)
            print('密钥字段已配置；仅固定样例可试验，真实素材准入仍关闭。此检查未发请求。')
            return 0
        if args.action == 'run':
            check_key(key)  # Reject before creating DB or billable records.
        with probe_lock(root):
            repo = Repository(root)
            try:
                if args.action == 'run':
                    value = run_probe(repo, key, authorized=True, retry_failed=args.retry_failed, new_revision=args.new_revision)
                else:
                    with repo.factory() as db:
                        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.kind == 'synthetic_deepseek').order_by(Job.created_at.desc())).first()
                        if not job:
                            print('尚无真实技术测试记录。'); return 2
                        jid = job.id
                    value = report(repo, jid)
                print(json.dumps({'state': value['state'], 'budget': value['budget'],
                    'calls': len(value['calls']), 'record': str(root / '技术测试/DeepSeek固定样例/技术调用记录.json')}, ensure_ascii=False))
                return 0 if value['state'] == 'succeeded' else 2
            finally:
                repo.engine.dispose()
    except (DomainError, DirectorError) as exc:
        print(exc.code + '：' + exc.message)
        return 2
    except KeyboardInterrupt:
        print('已中断；若已发送请求，预留继续占用，核对费用后处理。')
        return 2
    except Exception:
        print('本机准备或调用未完成；未输出密钥、请求正文或异常堆栈。')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
