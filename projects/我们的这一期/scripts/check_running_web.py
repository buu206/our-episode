#!/usr/bin/env python3
"""Check the actual local frontend before sending a preview link.

Read-only episode/media checks; creates and revokes only its own login session.
No cloud calls, media uploads, version changes or stored response bodies.
"""
import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

import httpx

PRODUCT = Path(__file__).resolve().parents[1]
FRONTEND = PRODUCT / 'frontend'
ORIGIN = 'http://127.0.0.1:3000'
API = '/api/v1'
IDENTIFIER = re.compile(r'^[a-f0-9]{32}$')


class CheckFailed(Exception):
    pass


class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = set()

    def handle_starttag(self, tag, attrs):
        if tag not in ('script', 'link'):
            return
        for key, value in attrs:
            if key in ('src', 'href') and value and value.startswith('/_next/static/'):
                self.paths.add(value)


def check_resources(client, html, static_root):
    parser = Resources()
    parser.feed(html)
    if not parser.paths:
        raise CheckFailed('网页没有正式构建资源，不能作为预览可用证明。')
    root = static_root.resolve()
    for url in sorted(parser.paths):
        relative = unquote(url.removeprefix('/_next/static/').split('?', 1)[0])
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise CheckFailed('正在运行的网页不是当前构建，请重新启动前端。')
        response = client.get(url)
        if response.status_code != 200 or hashlib.sha256(response.content).digest() != hashlib.sha256(path.read_bytes()).digest():
            raise CheckFailed('网页脚本或样式与当前构建不一致，请重新启动前端。')
    return len(parser.paths)


def check_episode_schema(payload):
    # Use the real maintained frontend contract, not a second Python schema.
    # Pass payload on stdin; never expose it or any zod input values in errors.
    code = """import fs from 'node:fs';
import { EpisodeSchema } from './src/lib/types.ts';
const result = EpisodeSchema.safeParse(JSON.parse(fs.readFileSync(0,'utf8')));
process.exit(result.success ? 0 : 2);
"""
    result = subprocess.run(['node', '--experimental-strip-types', '--input-type=module', '-e', code],
        input=json.dumps(payload), text=True, capture_output=True, timeout=30, cwd=FRONTEND)
    if result.returncode:
        raise CheckFailed('节目响应未通过当前前端数据契约，不能发送验收链接。')


def checked(response, status, label):
    if response.status_code != status:
        raise CheckFailed(f'{label}未通过（HTTP {response.status_code}）。')
    return response


def run(client, episode_id, version_id, invite):
    page = f'/episodes/{episode_id}'
    if version_id:
        page += f'?step=preview&version={version_id}'
    html = checked(client.get(page), 200, '网页').text
    count = check_resources(client, html, FRONTEND / '.next/static')
    logged_in = False
    try:
        auth = client.post(API + '/auth/login', json={'invite_code': invite})
        logged_in = auth.is_success
        checked(auth, 200, '本机登录')
        client.headers['X-CSRF-Token'] = auth.json()['csrf_token']
        episode = checked(client.get(f'{API}/episodes/{episode_id}'), 200, '节目读取').json()
        check_episode_schema(episode)
        if version_id:
            version = next((v for v in episode['versions'] if v['id'] == version_id), None)
            if not version or version['state'] not in ('ready', 'confirmed') or not version['media_url']:
                raise CheckFailed('指定候选尚不可播放，不能发送验收链接。')
            with client.stream('GET', f'{API}/versions/{version_id}/media', headers={'Range': 'bytes=0-1023'}) as media:
                checked(media, 206, '视频范围读取')
                # Next may stream the range using chunked transfer, without a
                # Content-Length. Validate the range and actual bytes instead.
                if not re.fullmatch(r'bytes 0-1023/[1-9][0-9]*', media.headers.get('content-range', '')) or len(media.read()) != 1024:
                    raise CheckFailed('视频范围读取长度异常。')
        return {'ok': True, 'current_build_resources': count, 'episode_schema': 'passed',
                'candidate_media': 'passed' if version_id else 'not_requested'}
    finally:
        try:
            if logged_in:
                checked(client.post(API + '/auth/logout'), 200, '退出检查会话')
        finally:
            client.cookies.clear()
            client.headers.pop('X-CSRF-Token', None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('episode')
    parser.add_argument('--version')
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.episode) or (args.version and not IDENTIFIER.fullmatch(args.version)):
        parser.error('节目及版本须为32位标识。')
    sys.path.insert(0, str(PRODUCT / 'backend'))
    from app.config import Settings
    settings = Settings.from_env()
    invite = next((k for k, v in settings.invites.items() if v == 'local-owner'), None)
    if not invite:
        print('未配置本机登录，无法验证完整节目响应。')
        return 1
    try:
        with httpx.Client(base_url=ORIGIN, headers={'Origin': ORIGIN}, trust_env=False, timeout=30) as client:
            print(json.dumps(run(client, args.episode, args.version, invite), ensure_ascii=False))
        return 0
    except CheckFailed as exc:
        print(str(exc))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, httpx.HTTPError):
        print('运行检查未完成，请检查本机服务及Node环境；不输出响应正文或凭据。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
