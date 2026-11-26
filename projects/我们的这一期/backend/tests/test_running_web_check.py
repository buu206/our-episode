"""Regression: HTTP 200 must not accept a stale or unusable frontend."""
import importlib.util
from pathlib import Path
import httpx
import pytest

spec = importlib.util.spec_from_file_location('running_web_check', Path(__file__).resolve().parents[2] / 'scripts/check_running_web.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_checks_encoded_route_resources_and_rejects_old_runtime(tmp_path):
    path = tmp_path / 'chunks/[id]/page.js'
    path.parent.mkdir(parents=True)
    path.write_text('current build')
    html = '<script src="/_next/static/chunks/%5Bid%5D/page.js"></script>'
    with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'current build'))) as client:
        assert checker.check_resources(client, html, tmp_path) == 1
        with pytest.raises(checker.CheckFailed, match='不是当前构建'):
            checker.check_resources(client, html.replace('page.js', 'old-page.js'), tmp_path)
    with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'old build'))) as client:
        with pytest.raises(checker.CheckFailed, match='不一致'):
            checker.check_resources(client, html, tmp_path)


def test_rejects_empty_page_and_encoded_path_escape(tmp_path):
    with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(lambda r: pytest.fail('must not request unsafe resource'))) as client:
        for html in ('<h1>200 OK</h1>', '<script src="/_next/static/%2e%2e/private.js"></script>'):
            with pytest.raises(checker.CheckFailed):
                checker.check_resources(client, html, tmp_path)


def test_failed_response_schema_still_logs_out_without_echo(monkeypatch):
    monkeypatch.setattr(checker, 'check_resources', lambda *a: 1)
    seen = []
    def transport(request):
        seen.append(request.url.path)
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'csrf_token': 'test-token'}, headers={'set-cookie': 'session=test-session'})
        if request.url.path.endswith('/logout'):
            assert request.headers['X-CSRF-Token'] == 'test-token'
            return httpx.Response(200, json={'ok': True})
        if request.url.path.startswith('/api/'):
            return httpx.Response(200, json={'unexpected': 'private-fixture-text'})
        return httpx.Response(200, text='page')
    with httpx.Client(base_url=checker.ORIGIN, transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(checker.CheckFailed, match='数据契约') as error:
            checker.run(client, 'a'*32, None, 'test-only')
        assert 'private-fixture-text' not in str(error.value)
        assert seen[-1].endswith('/logout')
        assert not client.cookies and 'X-CSRF-Token' not in client.headers


@pytest.mark.parametrize('range_header,size,accepted', [
    ('bytes 0-1023/4096', 1024, True),
    ('bytes 0-1023/4096', 100, False),
    ('bytes 1024-2047/4096', 1024, False),
])
def test_chunked_range_needs_correct_range_and_bytes(monkeypatch, range_header, size, accepted):
    monkeypatch.setattr(checker, 'check_resources', lambda *a: 1)
    monkeypatch.setattr(checker, 'check_episode_schema', lambda *a: None)
    vid = 'b'*32
    seen = []
    def transport(request):
        seen.append(request.url.path)
        if request.url.path.endswith('/login'):
            return httpx.Response(200, json={'csrf_token': 'test-token'})
        if request.url.path.endswith('/logout'):
            return httpx.Response(200, json={'ok': True})
        if request.url.path.endswith('/media'):
            return httpx.Response(206, stream=httpx.ByteStream(b'x'*size), headers={'content-range': range_header})
        if request.url.path.startswith('/api/'):
            return httpx.Response(200, json={'versions': [{'id': vid, 'state': 'ready', 'media_url': f'/api/v1/versions/{vid}/media'}]})
        return httpx.Response(200, text='page')
    with httpx.Client(base_url=checker.ORIGIN, transport=httpx.MockTransport(transport)) as client:
        if accepted:
            assert checker.run(client, 'a'*32, vid, 'test-only')['candidate_media'] == 'passed'
        else:
            with pytest.raises(checker.CheckFailed, match='长度异常'):
                checker.run(client, 'a'*32, vid, 'test-only')
        assert seen[-1].endswith('/logout')


from test_pipeline import client, settings, services, no_network, authenticate, request, episode_state


def test_real_backend_episode_passes_maintained_frontend_schema(client):
    auth = authenticate(client)
    created = request(client, '/episodes', auth, body={'title': '运行检查测试'}, expected=201)
    checker.check_episode_schema(episode_state(client, created['id']))
