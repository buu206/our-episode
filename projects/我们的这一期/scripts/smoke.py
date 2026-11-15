#!/usr/bin/env python3
"""Run one authorised cloud step, or observe an existing job without resubmitting.

Human evidence/narration approval remains in the UI. This utility never enables
cloud admission, directly writes the database, or treats demo/uncertain sends as a smoke.
"""
import argparse
from datetime import datetime
import fcntl
import hashlib
import hmac
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
import httpx
from dotenv.main import DotEnv
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from app.models import Budget, Episode, Job, ModelCall, Session as LoginSession, Version

STAGES = ('阶段2-1', '阶段2-2', '阶段2-3', '阶段3-1', '阶段3-2')
MODELS = {'vision': 'qwen3-vl-plus', 'directions': 'qwen3-vl-plus',
          'asr': 'fun-asr-flash-2026-06-15', 'tts': 'qwen3-tts-flash'}
ALLOWED_MODELS = {kind: {value} for kind, value in MODELS.items()}
ALLOWED_MODELS['vision'].add('deepseek-flash')
ALLOWED_MODELS['directions'].add('deepseek-flash')
ALLOWED_MODELS['asr'].add('volc.seedasr.sauc.duration')
ALLOWED_MODELS['asr'].add('asr-1.0')
ALLOWED_MODELS['tts'].add('seed-tts-2.0')
ALLOWED_MODELS['tts'].add('speech-2.8-hd')
ALLOWED_MODELS['director_chat'] = {'deepseek-flash'}
ALLOWED_MODELS['visual_review'] = {'deepseek-flash'}
CALL_KINDS = {'analysis': {'vision', 'asr'}, 'directions': {'directions'},
              'narration': {'tts'}, 'director_chat': {'director_chat'}, 'visual_review': {'visual_review'}}
TERMINAL = ('succeeded', 'failed')
WAIT_SECONDS = 900
ID = re.compile(r'[a-f0-9]{32}')
REQUEST_ID = re.compile(r'[A-Za-z0-9_.-]{1,128}')


class SmokeError(Exception):
    """Only fixed, non-sensitive messages may be printed by this utility."""


def configuration():
    # Match load_dotenv(override=False), including interpolation, without changing
    # this process's environment. Importing this module reads no configuration.
    disabled = os.environ.get('PYTHON_DOTENV_DISABLED', '').casefold() in ('1', 'true', 't', 'yes', 'y')
    values = {} if disabled else DotEnv(PRODUCT / 'backend/.env', override=False).dict()
    return {**{key: value for key, value in values.items() if value is not None}, **os.environ}


def require_admission(config):
    if any(config.get(key, 'bailian') != 'bailian' for key in
           ('VISION_PROVIDER', 'DIRECTOR_PROVIDER', 'ASR_PROVIDER', 'TTS_PROVIDER')):
        from app.services.provider_config import cloud_configuration
        from app.services.pipeline import ModelServices
        ready, _ = ModelServices(cloud_configuration(config)).ready()
        if not ready or not config.get('APP_INVITE_CODE'):
            raise SmokeError('所选供应商准入未就绪；不调用、不写冒烟记录。')
        return
    if (not all(isinstance(config.get(key), str) and config[key].lower() == 'true'
                for key in ('CLOUD_ENABLED', 'CLOUD_RETENTION_VERIFIED', 'CLOUD_REGION_VERIFIED'))
            or not config.get('MODEL_API_KEY') or not config.get('APP_INVITE_CODE')):
        raise SmokeError('真实模型准入未就绪；不调用、不写冒烟记录。')


def open_database(config):
    directory = Path(config.get('APP_DATA_DIR', str(PRODUCT / 'data'))).resolve()
    demo = (PRODUCT / 'data/demo').resolve()
    if directory == demo or demo in directory.parents:
        raise SmokeError('演示数据不能用于真实模型冒烟。')
    path = directory / 'app.sqlite3'
    if not directory.is_dir() or not path.is_file():
        raise SmokeError('配置的数据目录中没有现有数据库；不会创建空库。')
    # SQLite mode=ro is essential: create_engine alone can silently create a DB.
    uri = path.as_uri() + '?mode=ro'
    return create_engine('sqlite://', creator=lambda: sqlite3.connect(uri, uri=True, timeout=15))


def verify_session(engine, client, auth):
    token = client.cookies.get('episode_session')
    owner, csrf = auth.get('user_id'), auth.get('csrf_token')
    if not token or not isinstance(owner, str) or not owner or not isinstance(csrf, str) or not csrf:
        raise SmokeError('登录身份无法核对；不会提交任务或生成记录。')
    with Session(engine) as db:
        row = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        if (not row or row.owner_id != owner or row.expires <= time.time()
                or not hmac.compare_digest(row.csrf, csrf)):
            raise SmokeError('服务会话与配置数据库不一致；不会提交任务或生成记录。')
    client.headers['X-CSRF-Token'] = csrf
    return owner


def owned_job(db, job_id, owner, remote):
    job = db.get(Job, job_id)
    if (not job or job.owner_id != owner or job.kind not in CALL_KINDS
            or remote.get('id') != job.id or remote.get('kind') != job.kind
            or remote.get('version_id') != job.version_id):
        raise SmokeError('任务与当前身份或数据库不一致；不生成记录。')
    episode = db.get(Episode, job.episode_id)
    if not episode or episode.owner_id != owner:
        raise SmokeError('任务节目归属无法核对；不生成记录。')
    if job.version_id:
        version = db.get(Version, job.version_id)
        if (not version or version.owner_id != owner or version.episode_id != episode.id
                or version.origin == 'demo'):
            raise SmokeError('任务版本归属或来源无法核对；不生成记录。')
    return job


def check_target(engine, owner, step, target, remote):
    """Check ownership and persisted human confirmation before a paid POST."""
    with Session(engine) as db:
        if step == 'narration':
            version = db.get(Version, target)
            if (not version or version.owner_id != owner or version.origin == 'demo'
                    or remote.get('id') != target or not version.narration_approved
                    or remote.get('narration_approved') is not True
                    or remote.get('narration_text') != version.narration_text):
                raise SmokeError('请先在页面核对并确认此版旁白；脚本不会替你确认表达。')
            episode = db.get(Episode, version.episode_id)
        else:
            episode = db.get(Episode, target)
            if remote.get('id') != target:
                raise SmokeError('节目与配置数据库不一致。')
        if not episode or episode.owner_id != owner:
            raise SmokeError('节目不属于当前身份；不会提交任务。')
        if step == 'directions' and (
                episode.evidence_confirmed_revision != episode.evidence_revision
                or remote.get('evidence_revision') != episode.evidence_revision
                or remote.get('evidence_confirmed_revision') != episode.evidence_revision):
            raise SmokeError('请先在页面核对并确认证据；脚本不会代为确认。')


def _number(value):
    return str(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else '-'


def latest_source_timestamp(product):
    """Match verify.sh's source freshness scope without reading file contents."""
    excluded = {'.venv', 'node_modules', '.next', '__pycache__', '.pytest_cache', 'dist'}
    latest = 0.0
    for name in ('backend', 'frontend', 'scripts'):
        for directory, dirs, files in os.walk(product / name):
            dirs[:] = [item for item in dirs if item not in excluded]
            for filename in files:
                path = Path(directory) / filename
                if not path.is_symlink() and path.is_file():
                    latest = max(latest, path.stat().st_mtime)
    return latest


def record_real_calls(engine, job_id, owner, remote, stage, evidence_root):
    if stage not in STAGES:
        raise SmokeError('不支持的冒烟阶段。')
    rows_by_call, created_by_call = {}, {}
    with Session(engine) as db:
        job = owned_job(db, job_id, owner, remote)
        if job.state not in TERMINAL or remote.get('state') != job.state:
            return 0, 0
        database = next((row[2] for row in db.connection().exec_driver_sql('PRAGMA database_list')
                         if row[1] == 'main'), '')
        if not database:
            raise SmokeError('无法核对诊断记录的数据目录；不生成记录。')
        diagnostic_root = Path(database).resolve().parent / '真实AI诊断'
        calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job_id).order_by(ModelCall.created_at, ModelCall.id)))
        standard = job.state == 'succeeded' and bool(calls)
        for call in calls:
            if call.state not in TERMINAL or not isinstance(call.usage, dict) or call.usage.get('real_call') is not True:
                standard = False
                continue
            ledger = db.get(Budget, call.reservation_id)
            if (call.owner_id != owner or call.episode_id != job.episode_id
                    or call.kind not in CALL_KINDS[job.kind] or not ID.fullmatch(call.id)
                    or not ledger or ledger.owner_id != owner or ledger.episode_id != job.episode_id):
                raise SmokeError('调用记录或预算归属不一致；不生成记录。')
            settled = ledger.state == 'settled' and type(ledger.cost) is int and ledger.cost >= 0
            if call.state != 'succeeded' or not settled:
                standard = False
            # real_call means real adapter, NOT proof that a request was sent.
            # A provider response ID is the minimum evidence currently persisted.
            requests = call.usage.get('requests', [call.usage])
            if not isinstance(requests, list) or not requests:
                standard = False
                continue
            rows = []
            timestamp = datetime.fromisoformat(call.created_at)
            if timestamp.tzinfo is None:
                standard = False
                continue
            created_by_call[call.id] = timestamp.timestamp()
            timestamp = timestamp.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
            for usage in requests:
                if (not isinstance(usage, dict) or usage.get('cost_status') not in ('known', 'unknown')
                        or usage.get('model') not in ALLOWED_MODELS[call.kind]
                        or not isinstance(usage.get('request_id'), str)
                        or not REQUEST_ID.fullmatch(usage['request_id'])):
                    standard = False
                    continue
                schema = '结构合规，内容待打分' if call.state == 'succeeded' else '未通过或未完成'
                result = '瑕疵待打分' if call.state == 'succeeded' else '失败（真实请求，非通过）'
                version = usage.get('model_version')
                model_label = usage['model'] + ('（' + version + '）' if version in {'DeepSeek-V4.1-Flash-1017', 'deepseek-v4.1-flash-1017'} else '')
                row = [timestamp, call.kind + '/' + model_label, usage['request_id'],
                       _number(usage.get('input_tokens')), _number(usage.get('output_tokens')),
                       ('未采集首包时间（语音流式）' if usage.get('provider') == 'volc' else 'N/A（非流式）'), _number(call.elapsed) + 's（整组）', schema,
                       _number(usage.get('retries')), result, _number(usage.get('seconds')),
                       _number(usage.get('characters')), _number(usage.get('cost_yuan')),
                       _number(ledger.cost / 1e6) if settled else '-',
                       ('账本已结算' if usage['cost_status'] == 'known' else '账本已结算，供应商用量未知')
                       if settled else '待核对，保留预留']
                rows.append('| ' + ' | '.join(row) + ' |\n')
            if rows:
                rows_by_call[call.id] = rows
    if not rows_by_call:
        print('没有可核实的终态真实请求；未发送、进行中、演示或无请求凭据的记录不写入。')
        return 0, 0

    # A lock on this existing utility serialises cross-stage checks without a new
    # lock artifact or modifying any database/record when an export is refused.
    with Path(__file__).open('r', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = ''
        for other in STAGES:
            for root, filename in ((evidence_root, '冒烟记录.md'), (diagnostic_root, '调用记录.md')):
                candidate = root / other / filename
                contents = candidate.read_text(encoding='utf-8') if candidate.is_file() else ''
                if other == stage and (root == evidence_root) == standard:
                    existing = contents
                elif other != stage and any('<!-- call:' + call_id + ' -->' in contents for call_id in rows_by_call):
                    raise SmokeError('该批调用已归档到另一阶段；不能复制为新的阶段验证。')
        pending = {key: rows for key, rows in rows_by_call.items() if '<!-- call:' + key + ' -->' not in existing}
        if not pending:
            print('这些终态调用已经记录；未改写文件或刷新验证时间。')
            return len(rows_by_call) if standard else 0, 0
        newest_source = latest_source_timestamp(PRODUCT) if standard else 0
        if standard and any(created_by_call[call_id] < newest_source for call_id in pending):
            raise SmokeError('该任务调用后代码已有更新；可观察旧任务，但不能补写为当前代码的新冒烟证据。')
        if existing and '- 阶段：' + stage not in existing:
            raise SmokeError('既有记录阶段无法核对；不追加或改写。')
        output = (evidence_root / stage / '冒烟记录.md') if standard else (diagnostic_root / stage / '调用记录.md')
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('a', encoding='utf-8') as handle:
            if not existing:
                handle.write(('# 冒烟记录（smoke.py 自动写入，不得手填）\n' if standard
                              else '# 真实AI调用诊断（不计冒烟通过）\n') +
                    '- 产品：我们的这一期\n- 阶段：' + stage + '\n'
                    '- 模型：按能力配置的供应商（每行标注实际型号，已返回版本一并记录）\n'
                    '- 说明：供应商报价与账本结算分列；诊断不释放预算、不计通过，内容仍需人工评分。\n\n'
                    '| 时间 | 链路 | 请求 ID | 输入 token | 输出 token | 首字耗时 | 总耗时 | 结构校验 | 重试次数 | 结果 | 音频秒数 | 计费字符 | 供应商报价元 | 账本结算元（整组） | 费用状态 |\n'
                    '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n')
            for call_id, rows in pending.items():
                handle.writelines(rows)
                handle.write('<!-- job:' + job_id + ' -->\n<!-- call:' + call_id + ' -->\n')
            handle.flush()
            os.fsync(handle.fileno())
    print('已追加成功且费用结算的请求记录；内容效果仍待人工评分。' if standard
          else '真实请求仅归档到本机数据目录的诊断记录；任务或费用未通过，不计成功冒烟。')
    return len(rows_by_call) if standard else 0, len(pending)


def read_json(response):
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise SmokeError('本地接口响应无法核对；不输出原始内容。')
    return value


def run(client, engine, config, args):
    logged_in = False
    try:
        status = read_json(client.get('/status'))
        if (status.get('model_mode') != 'cloud'
                or (args.step != 'observe' and status.get('cloud_enabled') is not True)):
            raise SmokeError('当前服务不是已就绪的真实云端模式。')
        response = client.post('/auth/login', json={'invite_code': config['APP_INVITE_CODE']})
        # Even malformed successful login responses must reach logout cleanup.
        logged_in = 200 <= response.status_code < 300
        auth = read_json(response)
        if isinstance(auth.get('csrf_token'), str):
            client.headers['X-CSRF-Token'] = auth['csrf_token']
        owner = verify_session(engine, client, auth)
        if args.step == 'observe':
            job_id = args.target
        else:
            if args.step == 'narration':
                episodes = read_json(client.get('/episodes'))['items']
                remote = next((v for e in episodes for v in e['versions'] if v['id'] == args.target), {})
                check_target(engine, owner, args.step, args.target, remote)
                path, body = '/versions/' + args.target + '/narration', {'confirm_text': True}
            else:
                remote = read_json(client.get('/episodes/' + args.target))
                check_target(engine, owner, args.step, args.target, remote)
                path, body = '/episodes/' + args.target + '/' + args.step, {}
            job = read_json(client.post(path, json=body))
            job_id = job.get('id')
            if not isinstance(job_id, str) or not ID.fullmatch(job_id):
                raise SmokeError('任务提交结果未知；先在页面核查，不要重复提交。')
            with Session(engine) as db:
                row = owned_job(db, job_id, owner, job)
                if row.kind != args.step or (row.version_id if args.step == 'narration' else row.episode_id) != args.target:
                    raise SmokeError('返回任务与提交目标不一致；请核查，不要重复提交。')
        print('观察任务：' + job_id + '；中断后可使用 observe 和该 ID 继续，不重新收费提交。')
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            remote = read_json(client.get('/jobs/' + job_id))
            with Session(engine) as db:
                job = owned_job(db, job_id, owner, remote)
                complete = job.state in TERMINAL and remote.get('state') == job.state
            if complete:
                eligible, _ = record_real_calls(engine, job_id, owner, remote, args.stage, PRODUCT / 'docs/evidence')
                print('任务完成状态：' + job.state)
                return 0 if job.state == 'succeeded' and eligible else 1
            if time.monotonic() >= deadline:
                print('本地等待到期；任务仍按服务端状态执行。请用 observe 继续观察，不自动重新提交。')
                return 1
            time.sleep(1)
    finally:
        if logged_in:
            try:
                client.post('/auth/logout', json={}).raise_for_status()
            except httpx.HTTPError:
                print('本次脚本会话退出未确认；不会继续提交任务，请在本地核对会话。')
        client.cookies.clear()
        client.headers.pop('X-CSRF-Token', None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['analysis', 'directions', 'narration', 'observe'])
    parser.add_argument('target', help='节目ID；narration为已确认旁白的版本ID；observe为已有任务ID')
    parser.add_argument('--stage', choices=STAGES, default='阶段2-1', help='仅归档本次明确选择的一个阶段，默认阶段2-1')
    args = parser.parse_args(argv)
    if not ID.fullmatch(args.target):
        print('目标ID格式不正确。')
        return 2
    engine = None
    try:
        config = configuration()
        if args.step == 'observe':
            if not config.get('APP_INVITE_CODE'):
                raise SmokeError('观察已有任务仍需本机邀请码登录；不读取未授权记录。')
        else:
            require_admission(config)
        engine = open_database(config)
        with httpx.Client(base_url='http://127.0.0.1:8788/api/v1', trust_env=False,
                          follow_redirects=False, timeout=30) as client:
            return run(client, engine, config, args)
    except SmokeError as exc:
        print(str(exc))
        return 2
    except KeyboardInterrupt:
        print('已停止本地观察；未取消服务端任务，恢复时请使用 observe。')
        return 1
    except (httpx.HTTPError, SQLAlchemyError, OSError, ValueError, KeyError, TypeError):
        print('本地接口或记录核对未完成；先核查现有任务，不自动重提，不输出密钥或完整响应。')
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
