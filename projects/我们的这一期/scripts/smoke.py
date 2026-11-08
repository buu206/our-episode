#!/usr/bin/env python3
"""Future real-cloud smoke entry. Never confirms evidence or creates fake records.

Requires the separately authorised cloud configuration. The operator chooses an
existing episode/version. Human review remains in the UI between steps.
"""
import argparse
from datetime import datetime
import fcntl
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
import httpx
from dotenv import dotenv_values
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.models import ModelCall


def record_real_calls(job_id):
    engine = create_engine('sqlite:///' + str(PRODUCT / 'data/app.sqlite3'))
    with Session(engine) as db:
        calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job_id).order_by(ModelCall.created_at)))
    engine.dispose()
    calls = [call for call in calls if call.usage.get('real_call') is True]
    if not calls:
        print('没有实际模型调用记录；不创建冒烟记录。')
        return
    output = PRODUCT / 'docs/evidence/阶段2-1/冒烟记录.md'
    output.parent.mkdir(parents=True, exist_ok=True)
    def safe(value):
        return re.sub(r'[^A-Za-z0-9_.:/ -]', '', str(value))[:150] if value is not None else '-'
    models = sorted({safe(call.usage.get('model', call.kind)) for call in calls})
    with output.open('a+', encoding='utf-8') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        existing = handle.read()
        if not existing:
            handle.write('# 冒烟记录（smoke.py 自动写入，不得手填）\n'
                '- 产品：我们的这一期\n- 阶段：阶段2-1\n- 模型：阿里云百炼 / ' + ', '.join(models) + '\n'
                '- 写入时间：' + datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S') + '\n\n'
                '| 时间 | 链路 | 请求 ID | 输入 token | 输出 token | 首字耗时 | 总耗时 | 结构校验 | 重试次数 | 结果 |\n'
                '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n')
        for call in calls:
            marker = '<!-- call:' + call.id + ' -->'
            if marker in existing:
                continue
            timestamp = datetime.fromisoformat(call.created_at).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')
            requests = call.usage.get('requests') or [call.usage]
            for usage in requests:
                if not isinstance(usage, dict) or usage.get('cost_status') == 'not_sent':
                    continue
                # Contract checks alone do not establish semantic quality.
                result = '瑕疵待打分' if call.state == 'succeeded' else '失败'
                schema = '结构合规，内容待打分' if call.state == 'succeeded' else '未通过'
                row = [timestamp, safe(call.kind) + '/' + safe(usage.get('model', call.usage.get('model', '-'))),
                       safe(usage.get('request_id')), safe(usage.get('input_tokens')), safe(usage.get('output_tokens')),
                       'N/A（非流式）', safe(call.elapsed) + 's（整组）', schema, safe(usage.get('retries', 0)), result]
                handle.write('| ' + ' | '.join(row) + ' |\n')
            handle.write(marker + '\n')
        handle.flush()
    print('实际调用的脱敏记录已写入；模型内容质量仍待人工打分。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['analysis', 'directions', 'narration'])
    parser.add_argument('target', help='已有节目ID；narration 使用已核对旁白的版本ID')
    args = parser.parse_args()
    config = dotenv_values(PRODUCT / 'backend/.env')
    if not all(config.get(key, '').lower() == 'true' for key in
        ('CLOUD_ENABLED', 'CLOUD_RETENTION_VERIFIED', 'CLOUD_REGION_VERIFIED')) or not config.get('MODEL_API_KEY'):
        print('真实模型准入未就绪；按当前安排延至上线前。不调用、不写冒烟记录。')
        return 2
    if not re.fullmatch(r'[a-f0-9]{32}', args.target):
        print('目标ID格式不正确。')
        return 2
    job_id = None
    try:
        with httpx.Client(base_url='http://127.0.0.1:8788/api/v1', trust_env=False, timeout=30) as client:
            status = client.get('/status').json()
            if not status.get('cloud_enabled') or status.get('model_mode') != 'cloud':
                print('当前服务不是已就绪的真实云端模式。')
                return 2
            auth = client.post('/auth/login', json={'invite_code': config.get('APP_INVITE_CODE', '')})
            auth.raise_for_status()
            client.headers['X-CSRF-Token'] = auth.json()['csrf_token']
            if args.step == 'narration':
                # The existing UI confirmation is mandatory; this script must
                # not convert mere command execution into expression approval.
                episodes = client.get('/episodes').json()['items']
                target = next((v for e in episodes for v in e['versions'] if v['id'] == args.target), None)
                if not target or not target['narration_approved']:
                    print('请先在页面核对并确认该版旁白；脚本不会替你确认表达。')
                    return 2
                response = client.post('/versions/' + args.target + '/narration', json={'confirm_text': True})
            else:
                response = client.post('/episodes/' + args.target + '/' + args.step, json={})
            if response.status_code >= 400:
                print('未开始：' + response.json().get('error', {}).get('message', '接口未就绪'))
                return 2
            job_id = response.json()['id']
            deadline = time.monotonic() + 900
            while True:
                job = client.get('/jobs/' + job_id).json()
                if job['state'] in ('succeeded', 'failed'):
                    break
                if time.monotonic() > deadline:
                    print('本地等待到期；任务仍按服务端状态执行，不自动重新提交。')
                    return 1
                time.sleep(1)
            client.post('/auth/logout', json={})
            print('任务完成状态：' + job['state'])
            return 0 if job['state'] == 'succeeded' else 1
    except (httpx.HTTPError, ValueError, KeyError):
        print('本地接口未完成，请检查服务状态；不打印密钥或完整响应。')
        return 1
    finally:
        if job_id:
            record_real_calls(job_id)


if __name__ == '__main__':
    raise SystemExit(main())
