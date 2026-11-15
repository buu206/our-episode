"""One previously approved, non-personal fixed sample through the voice API.

Uses the same-origin web proxy and one stable client id. No live microphone,
new TTS, director-chat submission, production edit or unknown-result retry.
Public evidence contains technical metadata only.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.config import Settings

SOURCE = 'outputs/46a3eee5129c434bb8ee60d618ea7d64.wav'
SOURCE_SHA = 'ccb001bfddb11a9598335dd6eea6a0e6e8896485adfd5632794202d738fd90b9'
EXPECTED = '说好今晚不点外卖。结果，锅还没热，菜单先打开了。谁先破功？在场，开拍！'
CLIENT_ID = 'director-voice-fixed-20261113-v1'
TITLE = '导演语音固定合成样例技术测试'
BASE = 'http://127.0.0.1:3000'


def normalised(text):
    return ''.join(c for c in text if not unicodedata.category(c).startswith(('P', 'Z')))


def main():
    settings = Settings.from_env()
    source = settings.data_dir / SOURCE
    if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA:
        raise SystemExit('固定授权合成样例缺失或指纹不同，未发送。')
    directory = settings.data_dir / '技术测试/导演语音'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.smoke.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('此技术测试已在运行。') from None
        run(settings, source, directory)


def run(settings, source, directory):
    prepared = directory / 'approved-sample.webm'
    state_path = directory / '首测.json'
    if prepared.exists() and not state_path.exists():
        raise SystemExit('发现没有完整收据的准备文件，先核对，不自动提交。')
    if not prepared.exists():
        verified = source.read_bytes()
        if hashlib.sha256(verified).hexdigest() != SOURCE_SHA:
            raise SystemExit('固定样例指纹已变化，未发送。')
        copy = directory / 'verified-source.wav'
        with copy.open('xb') as output:
            output.write(verified)
        result = subprocess.run([shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg',
            '-nostdin', '-v', 'error', '-n', '-i', str(copy), '-vn', '-ac', '1',
            '-c:a', 'libopus', '-b:a', '64k', '-map_metadata', '-1', str(prepared)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise SystemExit('本机录音格式转换未完成，未发送。')
    payload = prepared.read_bytes()
    fingerprint = hashlib.sha256(payload).hexdigest()
    saved = json.loads(state_path.read_text()) if state_path.exists() else {}
    if saved and (saved.get('source_sha256') != SOURCE_SHA or saved.get('upload_sha256') != fingerprint):
        raise SystemExit('技术样例参数变化，不重复发送。')
    code = next((key for key, owner in settings.invites.items() if owner == 'local-owner'), None)
    if not code:
        raise SystemExit('本机账号未就绪，未发送。')
    with httpx.Client(base_url=BASE, headers={'Origin': BASE}, timeout=120) as client:
        def request(method, path, **kwargs):
            response = client.request(method, '/api/v1' + path, **kwargs)
            if response.status_code >= 400:
                try:
                    code = response.json().get('error', {}).get('code', 'ERROR')
                except ValueError:
                    code = 'INVALID_RESPONSE'
                raise SystemExit(f'接口拒绝：{response.status_code}/{code}；未自动重试。')
            return response.json()
        auth = request('POST', '/auth/login', json={'invite_code': code})
        client.headers['X-CSRF-Token'] = auth['csrf_token']
        if saved:
            episode_id = saved['episode_id']
        else:
            # Reconcile a creation interrupted before local receipt persistence.
            candidates = [e for e in request('GET', '/episodes')['items'] if e['title'] == TITLE]
            if len(candidates) > 1:
                raise SystemExit('同名技术任务不唯一，请先核对。')
            episode_id = (candidates[0] if candidates else request('POST', '/episodes', json={
                'title': TITLE, 'background': '既有固定合成旁白，仅验证语音输入链路，不是新真人录音。'}))['id']
            saved = {'episode_id': episode_id, 'client_id': CLIENT_ID,
                     'source_sha256': SOURCE_SHA, 'upload_sha256': fingerprint}
            temporary = state_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
            os.replace(temporary, state_path)
        route = '/episodes/' + episode_id + '/director/voice'
        status = request('GET', route + '/status')
        if not status['can_record'] or status['mode'] != 'cloud':
            raise SystemExit('真实语音链路未就绪，未发送。')
        previous = client.get('/api/v1' + route + '/by-client/' + CLIENT_ID)
        if previous.status_code == 404:
            # Exactly one explicit submission. An interrupted POST is never retried here.
            voice = request('POST', route, files={'file': ('director-voice.webm', payload, 'audio/webm')},
                data={'client_id': CLIENT_ID, 'confirm_send': 'true', 'accept_unknown_retention': 'true'})
        elif previous.status_code == 200:
            voice = previous.json()
        else:
            raise SystemExit(f'无法核对既有语音：{previous.status_code}，未发送。')
        print(json.dumps({'job_id': voice['job_id'], 'state': voice['state']}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + 180
        while voice['state'] in ('queued', 'running') and time.monotonic() < deadline:
            time.sleep(1)
            voice = request('GET', route + '/' + voice['id'])
        if voice['state'] != 'succeeded':
            raise SystemExit(f'语音任务{voice["state"]}，错误码：{(voice.get("error") or {}).get("code", "PENDING")}；未重发。')
        matched = normalised(voice['text'] or '') == normalised(EXPECTED)
        with sqlite3.connect(f'file:{settings.data_dir / "app.sqlite3"}?mode=ro', uri=True) as db:
            calls = db.execute('select created_at,usage,elapsed from model_calls where job_id=? and kind=?',
                               (voice['job_id'], 'director_voice')).fetchall()
        if len(calls) != 1:
            raise SystemExit('调用账目不唯一，停止并核对。')
        created, usage_json, elapsed = calls[0]
        usage = json.loads(usage_json)
        if not usage.get('real_call') or usage.get('cost_status') != 'known':
            raise SystemExit('调用费用尚不明确，停止并核对。')
        result = {'job_id': voice['job_id'], 'state': voice['state'], 'mode': voice['mode'],
                  'characters_match_excluding_punctuation': matched, 'usage': usage, 'elapsed_seconds': elapsed}
        (directory / '调用结果.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        evidence = ROOT / 'docs/evidence/阶段3-2/精灵语音实调记录.md'
        evidence.write_text('\n'.join([
            '# 精灵语音输入实调', '',
            '由 scripts/director_voice_smoke.py 自动写入。使用此前获批的非个人固定合成旁白，经本机转换为WebM、同源网页代理、规范化WAV后发送MiniMax；未录制用户麦克风，未将转写发给导演或修改任何成片。', '',
            '| 时间 | 模型 | 输入格式 | 耗时秒 | 费用元 | 真实调用 | 结构 | 文字核对 | 重试 |',
            '|---|---|---|---|---|---|---|---|---|',
            f'| {created} | {usage.get("model")} | WebM/Opus → WAV | {elapsed:.3f} | {usage.get("cost_yuan")} | 是 | 通过 | {"忽略标点一致" if matched else "有差异，待核对"} | {usage.get("retries", 0)} |', '',
            '记录与费用持久化；稳定请求编号用于结果对账，不自动重试未知结果。此证据只覆盖短语音技术链路，不替代真机麦克风、多人噪声、完整阶段AI冒烟或Q01成片质量验收。', '',
        ]))
        print(json.dumps(result, ensure_ascii=False))
        if not matched:
            raise SystemExit('真实转写完成，但文字与固定样例有差异，需核对。')


if __name__ == '__main__':
    main()
