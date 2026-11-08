"""HTTP boundaries for the local, single-creator stage.

The worker stays disabled so permission, quota and transition tests are
deterministic. Ready output is explicitly staged by the fixture; it is not a
claim that a real render or model call succeeded. Real media composition has
its own integration tests in test_media.py.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import shutil
import subprocess

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import Asset, Budget, Job, Session, Version
from app.services import media


BASE = "/api/v1"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "application-data",
        invites={"first-test": "alice", "second-test": "bob"},
        worker_enabled=False,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as connection:
        yield connection


@pytest.fixture(scope="session")
def video_bytes(tmp_path_factory) -> bytes:
    binary = shutil.which("ffmpeg")
    if not binary and Path("/opt/homebrew/bin/ffmpeg").is_file():
        binary = "/opt/homebrew/bin/ffmpeg"
    if not binary:
        pytest.skip("API media fixtures require the installed FFmpeg tool")
    source = tmp_path_factory.mktemp("api-real-media") / "source.mp4"
    subprocess.run(
        [binary, "-nostdin", "-v", "error", "-y", "-threads", "2",
         "-filter_threads", "1", "-f", "lavfi", "-i",
         "color=c=blue:size=64x64:rate=10", "-t", "0.5", "-an",
         "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(source)],
        check=True, capture_output=True, timeout=30, shell=False,
    )
    return source.read_bytes()


def login(client: TestClient, code="first-test") -> dict[str, str]:
    response = client.post(BASE + "/auth/login", json={"invite_code": code})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user_id"] == ("alice" if code == "first-test" else "bob")
    assert isinstance(payload["csrf_token"], str) and payload["csrf_token"]
    return {"X-CSRF-Token": payload["csrf_token"]}


@pytest.fixture
def headers(client: TestClient):
    return login(client)


def create_episode(client, headers, title="一起做饭", background="朋友聚会的测试记录"):
    response = client.post(BASE + "/episodes", headers=headers,
                           json={"title": title, "background": background})
    assert response.status_code in {200, 201}, response.text
    return response.json()


@pytest.fixture
def episode(client, headers):
    return create_episode(client, headers)


def upload(client, headers, episode_id, content: bytes, name="厨房片段.mp4"):
    return client.post(BASE + f"/episodes/{episode_id}/assets", headers=headers,
                       files={"file": (name, content, "video/mp4")})


@pytest.fixture
def asset(client, headers, episode, video_bytes):
    response = upload(client, headers, episode["id"], video_bytes)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def create_version(client, headers, episode_id, asset_id, **overrides):
    payload = {
        "title": "人工剪接第一版",
        "segments": [{"asset_id": asset_id, "start": 0, "end": 0.4,
                      "caption": "测试花字"}],
    }
    payload.update(overrides)
    return client.post(BASE + f"/episodes/{episode_id}/versions", headers=headers,
                       json=payload)


@pytest.fixture
def version(client, headers, episode, asset):
    response = create_version(client, headers, episode["id"], asset["id"])
    assert response.status_code in {200, 201}, response.text
    return response.json()


def assert_error(response, status=None):
    if status is not None:
        assert response.status_code == status, response.text
    else:
        assert 400 <= response.status_code < 500, response.text
    assert "application/json" in response.headers.get("content-type", "")
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message"}
    assert isinstance(payload["error"]["code"], str) and payload["error"]["code"]
    assert isinstance(payload["error"]["message"], str) and payload["error"]["message"]
    assert "Traceback" not in response.text
    assert "site-packages" not in response.text
    return payload["error"]


def get_episode(client, episode_id):
    response = client.get(BASE + f"/episodes/{episode_id}")
    assert response.status_code == 200, response.text
    return response.json()


def mark_ready(client, version_id, content):
    """Stage an existing output only for testing confirmation/authorization."""
    repo = client.app.state.repo
    relative = f"outputs/{version_id}.mp4"
    repo.path(relative).write_bytes(content)
    with repo.transaction() as db:
        saved = db.get(Version, version_id)
        saved.state = "ready"
        saved.output_path = relative
    return relative


def fast_probe(monkeypatch, duration=1.0):
    # Used only by quota tests. The true-type rejection test does not patch it.
    monkeypatch.setattr(media, "probe", lambda _path: {
        "duration": duration, "width": 64, "height": 64,
        "has_audio": False, "format": "mp4",
    })


def test_public_status_is_truthful_without_authentication(client):
    response = client.get(BASE + "/status")
    assert response.status_code == 200
    status = response.json()
    assert status["stage"] == "阶段3-1"
    assert status["cloud_enabled"] is False
    assert status["cloud_reason"]
    assert isinstance(status["media_ready"], bool)
    assert "first-test" not in response.text
    assert_error(client.get(BASE + "/episodes"), 401)


def test_login_uses_httponly_cookie_and_logout_invalidates_session(client):
    response = client.post(BASE + "/auth/login", json={"invite_code": "first-test"})
    assert response.status_code == 200
    cookie = response.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie or "samesite=strict" in cookie
    assert "path=/" in cookie
    token = response.json()["csrf_token"]
    assert client.get(BASE + "/auth/me").json() == response.json()
    old_cookies = dict(client.cookies)
    result = client.post(BASE + "/auth/logout", headers={"X-CSRF-Token": token}, json={})
    assert result.status_code in {200, 204}, result.text
    assert_error(client.get(BASE + "/auth/me"), 401)
    # A copied old session token must not work after logout.
    client.cookies.update(old_cookies)
    assert_error(client.get(BASE + "/auth/me"), 401)


def test_wrong_invite_does_not_create_an_authenticated_session(client):
    assert_error(client.post(BASE + "/auth/login", json={"invite_code": "wrong-code"}), 401)
    assert_error(client.get(BASE + "/auth/me"), 401)


def test_mutations_require_csrf_from_the_current_session(client, headers):
    payload = {"title": "不能绕过确认", "background": ""}
    assert_error(client.post(BASE + "/episodes", json=payload), 403)
    assert_error(client.post(BASE + "/episodes", json=payload,
                             headers={"X-CSRF-Token": "wrong-token"}), 403)
    response = client.post(BASE + "/episodes", json=payload, headers=headers)
    assert response.status_code in {200, 201}
    assert len(client.get(BASE + "/episodes").json()["items"]) == 1
    assert_error(client.post(BASE + "/auth/logout", json={}), 403)
    assert client.get(BASE + "/auth/me").status_code == 200


def test_second_user_cannot_read_or_mutate_episode_assets_versions_or_jobs(
    client, headers, episode, asset, version, video_bytes,
):
    render = client.post(BASE + f"/versions/{version['id']}/render", headers=headers, json={})
    assert render.status_code in {200, 201, 202}, render.text
    job_id = render.json()["id"]
    assert client.get(asset["media_url"]).content == video_bytes
    other = TestClient(client.app, raise_server_exceptions=False)
    try:
        bob = login(other, "second-test")
        assert other.get(BASE + "/episodes").json()["items"] == []
        for path in (BASE + f"/episodes/{episode['id']}", asset["media_url"],
                     BASE + f"/assets/{asset['id']}/impact", BASE + f"/jobs/{job_id}",
                     BASE + f"/versions/{version['id']}/export"):
            assert_error(other.get(path), 404)
        for path in (BASE + f"/versions/{version['id']}/render",
                     BASE + f"/versions/{version['id']}/confirm",
                     BASE + f"/episodes/{episode['id']}/directions"):
            assert_error(other.post(path, headers=bob, json={}), 404)
        assert_error(upload(other, bob, episode["id"], video_bytes), 404)
        assert_error(other.delete(BASE + f"/assets/{asset['id']}?confirm=true", headers=bob), 404)
        mine = get_episode(client, episode["id"])
        assert len(mine["assets"]) == 1
        assert len(mine["jobs"]) == 1
    finally:
        other.close()


def test_asset_from_another_episode_cannot_be_spliced_into_this_episode(
    client, headers, episode, asset,
):
    other = create_episode(client, headers, title="另一次聚会")
    response = create_version(client, headers, other["id"], asset["id"])
    assert_error(response)
    assert get_episode(client, other["id"])["versions"] == []


def test_fake_mp4_rejected_and_upload_scratch_is_removed(client, headers, episode, settings):
    private_content = b"not a movie; private-conversation-marker"
    response = upload(client, headers, episode["id"], private_content, name="private-context.mp4")
    assert_error(response)
    assert "private-conversation-marker" not in response.text
    assert str(settings.data_dir) not in response.text
    assert get_episode(client, episode["id"])["assets"] == []
    assert not [path for path in (settings.data_dir / "incoming").rglob("*") if path.is_file()]


def test_unsupported_filename_extension_rejected_even_with_real_mp4(
    client, headers, episode, video_bytes,
):
    assert_error(upload(client, headers, episode["id"], video_bytes, name="disguised.txt"))
    assert get_episode(client, episode["id"])["assets"] == []


def test_upload_path_and_unicode_name_are_safe(client, headers, episode, video_bytes, settings):
    response = upload(client, headers, episode["id"], video_bytes, name="../../聚会片段.mp4")
    # Rejecting traversal or reducing it to a safe display basename are both
    # acceptable; in neither case may it become an arbitrary storage path.
    if response.status_code >= 400:
        assert_error(response)
        assert get_episode(client, episode["id"])["assets"] == []
    else:
        result = response.json()
        assert result["filename"] == "聚会片段.mp4"
        with client.app.state.repo.transaction() as db:
            saved = db.get(Asset, result["id"])
            path = client.app.state.repo.path(saved.path)
            assert path.is_relative_to(settings.data_dir.resolve() / "assets")
            assert path.name != result["filename"]
        assert client.get(result["media_url"]).content == video_bytes
    assert not (settings.data_dir.parent / "聚会片段.mp4").exists()


def test_file_byte_limit_rejects_without_keeping_partial_upload(settings, video_bytes):
    limited = replace(settings, max_upload_bytes=len(video_bytes) - 1)
    with TestClient(create_app(limited), raise_server_exceptions=False) as local:
        auth = login(local)
        episode = create_episode(local, auth)
        assert_error(upload(local, auth, episode["id"], video_bytes))
        assert get_episode(local, episode["id"])["assets"] == []
        assert not [path for path in (settings.data_dir / "incoming").rglob("*") if path.is_file()]


def test_aggregate_byte_limit_applies_across_files(settings, video_bytes):
    limited = replace(settings, max_upload_bytes=len(video_bytes) * 2 - 1)
    with TestClient(create_app(limited), raise_server_exceptions=False) as local:
        auth = login(local)
        episode = create_episode(local, auth)
        first = upload(local, auth, episode["id"], video_bytes)
        assert first.status_code in {200, 201}
        assert_error(upload(local, auth, episode["id"], video_bytes, name="second.mp4"))
        assert len(get_episode(local, episode["id"])["assets"]) == 1


def test_episode_asset_count_limit_is_enforced(settings, video_bytes, monkeypatch):
    fast_probe(monkeypatch)
    with TestClient(create_app(replace(settings, max_assets=2)), raise_server_exceptions=False) as local:
        auth = login(local)
        episode = create_episode(local, auth)
        for index in range(2):
            assert upload(local, auth, episode["id"], video_bytes, name=f"{index}.mp4").status_code in {200, 201}
        assert_error(upload(local, auth, episode["id"], video_bytes, name="third.mp4"))
        assert len(get_episode(local, episode["id"])["assets"]) == 2


def test_total_source_duration_limit_is_enforced(settings, video_bytes, monkeypatch):
    fast_probe(monkeypatch, duration=301)
    with TestClient(create_app(settings), raise_server_exceptions=False) as local:
        auth = login(local)
        episode = create_episode(local, auth)
        assert upload(local, auth, episode["id"], video_bytes).status_code in {200, 201}
        assert_error(upload(local, auth, episode["id"], video_bytes, name="second.mp4"))
        assert len(get_episode(local, episode["id"])["assets"]) == 1


@pytest.mark.parametrize("start,end", [(-0.1, 0.4), (0.2, 0.2), (0.4, 0.2), (0, 1)])
def test_invalid_segment_range_cannot_be_saved(client, headers, episode, asset, start, end):
    response = create_version(client, headers, episode["id"], asset["id"],
                              segments=[{"asset_id": asset["id"], "start": start, "end": end,
                                         "caption": ""}])
    assert_error(response)
    assert get_episode(client, episode["id"])["versions"] == []


def test_total_output_limit_is_checked_before_any_render(client, headers, episode, video_bytes, monkeypatch):
    fast_probe(monkeypatch, duration=60)
    uploaded = upload(client, headers, episode["id"], video_bytes).json()
    response = create_version(client, headers, episode["id"], uploaded["id"],
                              segments=[{"asset_id": uploaded["id"], "start": 0, "end": 46,
                                         "caption": ""}] * 2)
    assert_error(response)
    saved = get_episode(client, episode["id"])
    assert saved["versions"] == []
    assert saved["jobs"] == []


def test_new_versions_preserve_previous_selection_and_manual_origin(client, headers, episode, asset, version):
    result = create_version(client, headers, episode["id"], asset["id"], title="改一下花字",
                            base_version_id=version["id"],
                            segments=[{"asset_id": asset["id"], "start": 0.1, "end": 0.4,
                                       "caption": "新的花字"}])
    assert result.status_code in {200, 201}, result.text
    updated = result.json()
    assert updated["id"] != version["id"]
    assert updated["origin"] == "manual"
    assert updated["base_version_id"] == version["id"]
    stored = {item["id"]: item for item in get_episode(client, episode["id"])["versions"]}
    assert len(stored) == 2
    assert stored[version["id"]]["segments"] == version["segments"]
    assert stored[version["id"]]["state"] == "draft"


def test_draft_cannot_be_confirmed_or_exported(client, headers, episode, version):
    assert_error(client.post(BASE + f"/versions/{version['id']}/confirm", headers=headers, json={}))
    assert_error(client.get(BASE + f"/versions/{version['id']}/export"))
    assert get_episode(client, episode["id"])["versions"][0]["state"] == "draft"


def test_ready_version_requires_explicit_confirmation_before_export(
    client, headers, episode, version, video_bytes,
):
    mark_ready(client, version["id"], video_bytes)
    before = get_episode(client, episode["id"])["versions"][0]
    assert before["state"] == "ready"
    assert before["media_url"]
    assert client.get(before["media_url"]).content == video_bytes
    assert_error(client.get(BASE + f"/versions/{version['id']}/export"))
    confirmed = client.post(BASE + f"/versions/{version['id']}/confirm", headers=headers, json={})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["state"] == "confirmed"
    exported = client.get(BASE + f"/versions/{version['id']}/export")
    assert exported.status_code == 200
    assert exported.content == video_bytes
    assert "video/mp4" in exported.headers.get("content-type", "")
    assert "attachment" in exported.headers.get("content-disposition", "")
    other = TestClient(client.app, raise_server_exceptions=False)
    try:
        login(other, "second-test")
        assert_error(other.get(before["media_url"]), 404)
        assert_error(other.get(BASE + f"/versions/{version['id']}/export"), 404)
    finally:
        other.close()


def test_repeated_render_reuses_the_same_queued_job(client, headers, episode, version):
    route = BASE + f"/versions/{version['id']}/render"
    first = client.post(route, headers=headers, json={})
    second = client.post(route, headers=headers, json={})
    assert first.status_code in {200, 201, 202}, first.text
    assert second.status_code in {200, 201, 202}, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["state"] == second.json()["state"] == "queued"
    assert client.get(BASE + f"/jobs/{first.json()['id']}").json()["id"] == first.json()["id"]
    saved = get_episode(client, episode["id"])
    assert len(saved["jobs"]) == 1
    assert saved["versions"][0]["state"] == "rendering"


def test_delete_requires_confirmation_and_revokes_dependent_preview_and_export(
    client, headers, episode, asset, version, video_bytes,
):
    mark_ready(client, version["id"], video_bytes)
    confirmed = client.post(BASE + f"/versions/{version['id']}/confirm", headers=headers, json={})
    assert confirmed.status_code == 200
    old_output_url = confirmed.json()["media_url"]
    impact = client.get(BASE + f"/assets/{asset['id']}/impact")
    assert impact.status_code == 200
    assert impact.json()["versions"] == [version["id"]]
    assert_error(client.delete(BASE + f"/assets/{asset['id']}", headers=headers))
    assert client.get(asset["media_url"]).status_code == 200
    response = client.delete(BASE + f"/assets/{asset['id']}?confirm=true", headers=headers)
    assert response.status_code in {200, 204}, response.text
    saved = get_episode(client, episode["id"])
    assert saved["assets"] == []
    assert saved["versions"][0]["state"] == "restricted"
    assert_error(client.get(asset["media_url"]))
    assert_error(client.get(old_output_url))
    assert_error(client.get(BASE + f"/versions/{version['id']}/export"))
    assert_error(client.post(BASE + f"/versions/{version['id']}/render", headers=headers, json={}))
    assert_error(client.post(BASE + f"/versions/{version['id']}/confirm", headers=headers, json={}))
    with client.app.state.repo.transaction() as db:
        saved_asset = db.get(Asset, asset["id"])
        assert saved_asset.deleted
        assert not client.app.state.repo.path(saved_asset.path).exists()


def test_delete_unlink_failure_is_recoverable_by_repeating_delete(
    client, headers, episode, asset, version, video_bytes, monkeypatch,
):
    relative_output = mark_ready(client, version["id"], video_bytes)
    repo = client.app.state.repo
    with repo.factory() as db:
        source_path = repo.path(db.get(Asset, asset["id"]).path)
    output_path = repo.path(relative_output)
    original_unlink = Path.unlink

    def fail_source(path, *args, **kwargs):
        if path == source_path:
            raise PermissionError("private-path must not be exposed")
        return original_unlink(path, *args, **kwargs)

    route = BASE + f"/assets/{asset['id']}?confirm=true"
    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", fail_source)
        failed = client.delete(route, headers=headers)
    error = assert_error(failed, 503)
    assert error["code"] == "DELETION_PENDING"
    assert "private-path" not in failed.text
    assert str(source_path) not in failed.text
    assert source_path.exists()
    assert not output_path.exists()
    assert_error(client.get(asset["media_url"]), 404)
    assert_error(client.get(BASE + f"/versions/{version['id']}/media"), 409)
    assert get_episode(client, episode["id"])["versions"][0]["state"] == "restricted"

    retry = client.delete(route, headers=headers)
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"ok": True}
    assert not source_path.exists()
    assert client.delete(route, headers=headers).status_code == 200


def test_restart_retries_only_persistently_marked_deletions(settings, video_bytes, monkeypatch, caplog):
    with TestClient(create_app(settings), raise_server_exceptions=False) as first:
        auth = login(first)
        episode = create_episode(first, auth)
        asset = upload(first, auth, episode["id"], video_bytes).json()
        retained = upload(first, auth, episode["id"], video_bytes, name="keep.mp4").json()
        version = create_version(first, auth, episode["id"], asset["id"]).json()
        output_relative = mark_ready(first, version["id"], video_bytes)
        repo = first.app.state.repo
        with repo.factory() as db:
            source_path = repo.path(db.get(Asset, asset["id"]).path)
            retained_path = repo.path(db.get(Asset, retained["id"]).path)
        output_path = repo.path(output_relative)
        unmarked = repo.path("outputs/unmarked-file.mp4")
        unmarked.write_bytes(b"unmarked files must not be swept")
        original_unlink = Path.unlink

        def fail_marked(path, *args, **kwargs):
            if path in (source_path, output_path):
                raise PermissionError("simulated filesystem failure")
            return original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(Path, "unlink", fail_marked)
            failed = first.delete(BASE + f"/assets/{asset['id']}?confirm=true", headers=auth)
            assert assert_error(failed, 503)["code"] == "DELETION_PENDING"
        assert source_path.exists() and output_path.exists()
        cookies = dict(first.cookies)

    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", fail_marked)
        with TestClient(create_app(settings), raise_server_exceptions=False) as still_pending:
            assert still_pending.get(BASE + "/status").status_code == 200
            assert still_pending.app.state.pending_cleanup == 2
            assert source_path.exists() and output_path.exists()
    assert "Local deletion cleanup pending: 2 application files" in caplog.text
    assert str(source_path) not in caplog.text
    assert "simulated filesystem failure" not in caplog.text

    with TestClient(create_app(settings), raise_server_exceptions=False) as restarted:
        restarted.cookies.update(cookies)
        assert not source_path.exists()
        assert not output_path.exists()
        assert retained_path.read_bytes() == video_bytes
        assert unmarked.read_bytes() == b"unmarked files must not be swept"
        saved = get_episode(restarted, episode["id"])
        assert [item["id"] for item in saved["assets"]] == [retained["id"]]
        assert saved["versions"][0]["state"] == "restricted"
        assert_error(restarted.get(asset["media_url"]), 404)
        assert_error(restarted.get(BASE + f"/versions/{version['id']}/media"), 409)


def test_cloud_block_does_not_create_jobs_reservations_or_charges(client, headers, episode):
    before = get_episode(client, episode["id"])["budget"]
    response = client.post(BASE + f"/episodes/{episode['id']}/directions", headers=headers, json={})
    assert_error(response)
    after = get_episode(client, episode["id"])
    assert after["directions"] == []
    assert after["jobs"] == []
    assert after["budget"] == before
    assert after["budget"]["spent"] == after["budget"]["reserved"] == 0
    with client.app.state.repo.transaction() as db:
        assert db.scalar(select(func.count()).select_from(Budget)) == 0
        assert db.scalar(select(func.count()).select_from(Job)) == 0


def test_validation_and_unknown_route_errors_are_structured_without_private_input(client, headers):
    secret_marker = "private-document-do-not-echo"
    response = client.post(BASE + "/episodes", headers=headers,
                           json={"title": "", "background": secret_marker, "unexpected": secret_marker})
    assert_error(response, 422)
    assert secret_marker not in response.text
    assert_error(client.get(BASE + "/route-that-does-not-exist"), 404)
    assert client.get(BASE + "/episodes").json()["items"] == []


def test_missing_asset_errors_are_safe_and_leave_no_version(client, headers, episode):
    response = create_version(client, headers, episode["id"], "not-an-existing-asset")
    assert_error(response)
    assert get_episode(client, episode["id"])["versions"] == []


def test_unexpected_processing_exception_is_sanitized_and_cleans_upload(
    client, headers, episode, video_bytes, settings, monkeypatch,
):
    def crash(_path):
        raise RuntimeError("private-media-details /private/user-documents secret-marker")

    monkeypatch.setattr(media, "probe", crash)
    response = upload(client, headers, episode["id"], video_bytes)
    assert_error(response, 500)
    assert "private-media-details" not in response.text
    assert "secret-marker" not in response.text
    assert "RuntimeError" not in response.text
    assert get_episode(client, episode["id"])["assets"] == []
    assert not [path for path in (settings.data_dir / "incoming").rglob("*") if path.is_file()]


def test_database_recreation_retains_session_assets_versions_and_pending_job(settings, video_bytes):
    with TestClient(create_app(settings), raise_server_exceptions=False) as first:
        auth = login(first)
        episode = create_episode(first, auth, title="重启前的这一期")
        uploaded = upload(first, auth, episode["id"], video_bytes)
        assert uploaded.status_code in {200, 201}
        asset = uploaded.json()
        created = create_version(first, auth, episode["id"], asset["id"])
        assert created.status_code in {200, 201}
        version = created.json()
        job = first.post(BASE + f"/versions/{version['id']}/render", headers=auth, json={})
        assert job.status_code in {200, 201, 202}
        job_id = job.json()["id"]
        cookies = dict(first.cookies)
    # This is a new ASGI application and repository against the same disk data.
    with TestClient(create_app(settings), raise_server_exceptions=False) as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get(BASE + "/auth/me").json()["user_id"] == "alice"
        saved = get_episode(restarted, episode["id"])
        assert saved["title"] == "重启前的这一期"
        assert saved["assets"][0]["id"] == asset["id"]
        assert saved["versions"][0]["id"] == version["id"]
        assert saved["versions"][0]["segments"] == version["segments"]
        assert saved["jobs"][0]["id"] == job_id
        assert saved["jobs"][0]["state"] == "queued"
        assert restarted.get(asset["media_url"]).content == video_bytes


def test_interrupted_running_job_becomes_retryable_without_overwriting_input(
    client, headers, episode, version,
):
    queued = client.post(BASE + f"/versions/{version['id']}/render", headers=headers, json={})
    assert queued.status_code in {200, 201, 202}
    job_id = queued.json()["id"]
    with client.app.state.repo.transaction() as db:
        db.get(Job, job_id).state = "running"
    client.app.state.repo.recover()
    saved = get_episode(client, episode["id"])
    assert saved["jobs"][0]["state"] == "failed"
    assert saved["jobs"][0]["error"]["code"] == "INTERRUPTED"
    assert saved["versions"][0]["state"] == "failed"
    assert saved["versions"][0]["segments"] == version["segments"]
    retried = client.post(BASE + f"/versions/{version['id']}/render", headers=headers, json={})
    assert retried.status_code in {200, 201, 202}
    assert retried.json()["id"] != job_id
    assert retried.json()["state"] == "queued"
    assert len(get_episode(client, episode["id"])["jobs"]) == 2


def test_expired_session_is_rejected_server_side(client, headers):
    with client.app.state.repo.transaction() as db:
        for session in db.scalars(select(Session)):
            session.expires = 0
    assert_error(client.get(BASE + "/auth/me"), 401)
    assert_error(client.post(BASE + "/episodes", headers=headers,
                             json={"title": "会话已过期", "background": ""}), 401)
