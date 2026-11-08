import asyncio
from app.body_limit import BodyLimitMiddleware


def exercise(chunks, declared=None):
    async def run():
        reached = []
        messages = []
        async def endpoint(scope, receive, send):
            while True:
                message = await receive()
                reached.append(len(message.get('body', b'')))
                if not message.get('more_body'):
                    break
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})
        iterator = iter(chunks)
        async def receive():
            return next(iterator)
        async def send(message):
            messages.append(message)
        headers = [(b'content-length', str(declared).encode())] if declared is not None else []
        await BodyLimitMiddleware(endpoint, 100)(
            {'type': 'http', 'method': 'POST', 'path': '/api/v1/episodes', 'headers': headers}, receive, send)
        return messages, reached
    return asyncio.run(run())


def test_declared_limit_before_reading():
    messages, reached = exercise([], 65537)
    assert messages[0]['status'] == 413 and not reached


def test_chunked_limit_without_content_length():
    messages, reached = exercise([{'type': 'http.request', 'body': b'x' * 40000, 'more_body': True},
                                  {'type': 'http.request', 'body': b'x' * 30000, 'more_body': False}])
    assert messages[0]['status'] == 413 and reached == [40000]


def test_small_request_passes_unchanged():
    messages, reached = exercise([{'type': 'http.request', 'body': b'{}'}], 2)
    assert messages[0]['status'] == 200 and reached == [2]


def test_oversized_json_rejected_in_real_app(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    with TestClient(create_app(Settings(data_dir=tmp_path, worker_enabled=False))) as client:
        response = client.post('/api/v1/auth/login', json={'invite_code': 'x' * 70000})
        assert response.status_code == 413
        assert set(response.json()) == {'error'}


def test_chunked_json_rejected_in_real_app(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    with TestClient(create_app(Settings(data_dir=tmp_path, worker_enabled=False))) as client:
        response = client.post('/api/v1/auth/login',
            content=(chunk for chunk in [b'{"invite_code":"', b'x' * 70000, b'"}']),
            headers={'Content-Type': 'application/json'})
        assert response.status_code == 413
        assert set(response.json()) == {'error'}
