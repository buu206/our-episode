from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


def test_local_entry_retains_cookie_csrf_and_owner_isolation(tmp_path):
    settings = Settings(data_dir=tmp_path, worker_enabled=False, local_demo_entry=True,
                        invites={'other-invite': 'other-owner'})
    with TestClient(create_app(settings), client=('127.0.0.1', 5000)) as client:
        assert client.get('/api/v1/auth/me').status_code == 401
        auth = client.get('/api/v1/auth/me', headers={'x-local-demo-entry': '1'})
        assert auth.status_code == 200
        assert 'HttpOnly' in auth.headers['set-cookie']
        assert auth.json()['user_id'] == 'local-owner'
        assert client.get('/api/v1/auth/me').json() == auth.json()
        assert client.post('/api/v1/episodes', json={'title': 'sample'}).status_code == 403
        created = client.post('/api/v1/episodes', json={'title': 'sample'},
                              headers={'x-csrf-token': auth.json()['csrf_token']})
        assert created.status_code == 201
        other = client.post('/api/v1/auth/login', json={'invite_code': 'other-invite'})
        assert other.status_code == 200
        assert client.get('/api/v1/auth/me', headers={'x-local-demo-entry': '1'}).json()['user_id'] == 'other-owner'
        assert client.get('/api/v1/episodes/' + created.json()['id']).status_code == 404


def test_entry_disabled_by_default_or_for_remote_client(tmp_path):
    for enabled, host in [(False, '127.0.0.1'), (True, '192.168.1.20')]:
        settings = Settings(data_dir=tmp_path / host / str(enabled), worker_enabled=False, local_demo_entry=enabled)
        with TestClient(create_app(settings), client=(host, 5000)) as client:
            assert client.get('/api/v1/auth/me', headers={'x-local-demo-entry': '1'}).status_code == 401


def test_local_entry_rejects_cross_site_requests(tmp_path):
    settings = Settings(data_dir=tmp_path, worker_enabled=False, local_demo_entry=True)
    with TestClient(create_app(settings), client=('127.0.0.1', 5000)) as client:
        for extra in [{'origin': 'https://evil.example'}, {'sec-fetch-site': 'cross-site'}]:
            assert client.get('/api/v1/auth/me', headers={'x-local-demo-entry': '1', **extra}).status_code == 401
