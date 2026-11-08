#!/usr/bin/env python3
"""Exercise the running local service with clearly labelled synthetic footage.
No model invocation. Credentials stay in process memory and are never printed.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import sys
import httpx
from dotenv import dotenv_values

PRODUCT = Path(__file__).resolve().parents[1]


def main():
    browser_fixture = '--browser-fixture' in sys.argv
    config = {'APP_INVITE_CODE': 'browser-test'} if browser_fixture else dotenv_values(PRODUCT / 'backend/.env')
    with tempfile.TemporaryDirectory(prefix='episode-check-') as temp, httpx.Client(
            base_url='http://127.0.0.1:8790' if browser_fixture else 'http://127.0.0.1:8788', timeout=60, trust_env=False) as client:
        source = Path(temp) / '程序生成的验收片段.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=640x360:rate=25:duration=5', '-f', 'lavfi', '-i',
                        'sine=frequency=440:sample_rate=48000:duration=5', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(source)], check=True, timeout=30)

        def request(method, path, **kwargs):
            response = client.request(method, '/api/v1' + path, **kwargs)
            if response.status_code >= 400:
                # The server contract contains only safe error codes/messages.
                raise RuntimeError(str(response.status_code) + ' ' + str(response.json()))
            return response

        auth = request('POST', '/auth/login', json={'invite_code': config['APP_INVITE_CODE']}).json()
        client.headers['X-CSRF-Token'] = auth['csrf_token']
        status = request('GET', '/status').json()
        assert status['media_ready'] and not status['cloud_enabled']
        episode = request('POST', '/episodes', json={
            'title': '本地验收 · 程序生成测试片段',
            'background': '这是一段测试图案与合成声音，不含真实人物，不是 AI 生成的节目。'}).json()
        with source.open('rb') as handle:
            asset = request('POST', f"/episodes/{episode['id']}/assets",
                            files={'file': (source.name, handle, 'video/mp4')}).json()
        version = request('POST', f"/episodes/{episode['id']}/versions", json={
            'title': '本地出片链路验收',
            'segments': [{'asset_id': asset['id'], 'start': .5, 'end': 2.5, 'caption': '本地合成测试 · 非 AI 结果'},
                         {'asset_id': asset['id'], 'start': 3, 'end': 5, 'caption': '原声保留 / 花字 / 720p'}]}).json()
        job = request('POST', f"/versions/{version['id']}/render", json={}).json()
        duplicate = request('POST', f"/versions/{version['id']}/render", json={}).json()
        assert duplicate['id'] == job['id']
        deadline = time.monotonic() + 120
        while job['state'] not in ('succeeded', 'failed'):
            if time.monotonic() > deadline:
                raise RuntimeError('本地渲染超时')
            time.sleep(.3)
            job = request('GET', f"/jobs/{job['id']}").json()
        assert job['state'] == 'succeeded', job.get('error')
        preview = request('GET', f"/versions/{version['id']}/media")
        assert preview.headers['content-type'] == 'video/mp4'
        confirmed = request('POST', f"/versions/{version['id']}/confirm", json={}).json()
        assert confirmed['state'] == 'confirmed'
        result = request('GET', f"/versions/{version['id']}/export")
        exported = Path(temp) / 'export.mp4'
        exported.write_bytes(result.content)
        output = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                            '-show_format', '-of', 'json', str(exported)], timeout=15))
        video = next(s for s in output['streams'] if s['codec_type'] == 'video')
        assert (video['width'], video['height']) == (1280, 720)
        assert 3.8 <= float(output['format']['duration']) <= 4.2
        assert any(s['codec_type'] == 'audio' for s in output['streams'])
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(exported), '-f', 'null', '-'], check=True, timeout=30)
        request('POST', '/auth/logout', json={})
        summary = {'episode_id': episode['id'], 'version_id': version['id'], 'job_state': job['state'],
                   'export_bytes': len(result.content), 'width': video['width'], 'height': video['height'],
                   'duration': float(output['format']['duration']), 'audio': True, 'full_decode': True,
                   'source': 'program-generated test pattern and sine wave', 'real_model_called': False}
        if not browser_fixture:
            (PRODUCT / 'data/local-check.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
