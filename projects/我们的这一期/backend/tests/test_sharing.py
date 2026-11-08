"""Offline authorization/hash/FD tests, complementary to real-render integration.

The output bytes here deliberately are not an encoded film. They test immutable
publication and file access only; test_sharing_integration covers real FFmpeg.
"""
import json
import os
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Version, now, uid
from app.repository import DomainError
from app.services import sharing as service
from app.sharing_models import Share, ShareOperation, ViewerSession
from test_collaboration import BASE, h  # Isolated app, no worker/model calls.


def saved_version(h, *, episode=None, content=b"offline-published-output"):
    relative = "outputs/" + uid() + ".mp4"
    h.repo.path(relative).write_bytes(content)
    with h.repo.transaction() as db:
        version = Version(owner_id="alice", episode_id=episode or h.episode, title="固定标题",
            state="confirmed", confirmed_at=now(), output_path=relative, packaging="original",
            segments=[{"asset_id": h.source, "start": 0, "end": 1, "caption": ""}])
        db.add(version)
        db.flush()
        return version.id, relative


def create(h, version, **overrides):
    body = {"label": "朋友观看", "confirmed": True, "expires_days": 3, "client_request_id": uid()}
    body.update(overrides)
    response = h.client.post(BASE + f"/versions/{version}/shares", headers=h.owner, json=body)
    assert response.status_code == 201
    return response.json(), body


def viewer(h, share):
    response = h.client.post(BASE + "/viewer/auth/login", json={
        "share_id": share["id"], "share_code": share["share_code"]})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def update_body(version, revision=1, **overrides):
    return {"version_id": version, "expected_revision": revision,
            "confirmed": True, "client_request_id": uid(), **overrides}


def test_creation_idempotency_never_stores_or_replays_plaintext_code(h):
    version, _ = saved_version(h)
    share, body = create(h, version)
    replay = h.client.post(BASE + f"/versions/{version}/shares", headers=h.owner, json=body)
    assert replay.status_code == 201
    assert replay.json()["id"] == share["id"]
    assert replay.json()["share_code"] is None and replay.json()["watch_url"] is None
    conflict = h.client.post(BASE + f"/versions/{version}/shares", headers=h.owner,
                            json={**body, "label": "different"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with h.repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Share)) == 1
        row = db.get(Share, share["id"])
        assert row.code_hash == service.digest(share["share_code"])
        operation = db.scalars(select(ShareOperation)).one()
        assert share["share_code"] not in json.dumps(operation.result)
        assert "share_code" not in operation.result and "watch_url" not in operation.result


def test_update_replay_cannot_publish_twice_or_extend_expiry(h):
    version, _ = saved_version(h)
    share, _ = create(h, version)
    route, body = BASE + f"/shares/{share['id']}/update", update_body(version)
    first = h.client.post(route, headers=h.owner, json=body)
    replay = h.client.post(route, headers=h.owner, json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["revision"] == 2
    assert first.json()["expires_at"] == share["expires_at"]
    conflict = h.client.post(route, headers=h.owner, json={**body, "expected_revision": 2})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("terminal", ["closed", "expired"])
def test_replaying_update_does_not_resurrect_terminal_grant(h, terminal):
    version, _ = saved_version(h)
    share, _ = create(h, version)
    route, body = BASE + f"/shares/{share['id']}/update", update_body(version)
    assert h.client.post(route, headers=h.owner, json=body).status_code == 200
    if terminal == "closed":
        close = h.client.post(BASE + f"/shares/{share['id']}/close", headers=h.owner,
                              json={"expected_revision": 2, "confirmed": True})
        assert close.status_code == 200
    else:
        with h.repo.transaction() as db:
            db.get(Share, share["id"]).expires_at = time.time() - 1
    replay = h.client.post(route, headers=h.owner, json=body)
    assert replay.status_code == 410
    with h.repo.factory() as db:
        assert db.get(Share, share["id"]).state == terminal


def test_old_update_replay_cannot_clear_later_restriction(h):
    version, _ = saved_version(h)
    share, _ = create(h, version)
    route, body = BASE + f"/shares/{share['id']}/update", update_body(version)
    assert h.client.post(route, headers=h.owner, json=body).status_code == 200
    with h.repo.transaction() as db:
        assert service.suspend_version_shares(db, [version], "TEST_REVOKED") == 1
    replay = h.client.post(route, headers=h.owner, json=body)
    assert replay.status_code == 200
    assert replay.json()["state"] == "restricted" and replay.json()["revision"] == 2
    fresh = h.client.post(route, headers=h.owner, json=update_body(version, 2))
    assert fresh.status_code == 200
    assert fresh.json()["state"] == "active" and fresh.json()["revision"] == 3
    assert fresh.json()["expires_at"] == share["expires_at"]


def test_idempotency_conflict_still_commits_discovered_snapshot_restriction(h):
    version, path = saved_version(h)
    share, create_body = create(h, version)
    h.repo.path(path).write_bytes(b"different published bytes")
    response = h.client.post(BASE + f"/shares/{share['id']}/update", headers=h.owner,
        json=update_body(version, client_request_id=create_body["client_request_id"]))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with h.repo.factory() as db:
        assert db.get(Share, share["id"]).state == "restricted"


def test_same_length_content_change_is_durable_and_does_not_auto_restore(h):
    original = b"original-fixed-bytes"
    version, path = saved_version(h, content=original)
    share, _ = create(h, version)
    viewer(h, share)
    h.repo.path(path).write_bytes(b"x" * len(original))
    route = BASE + f"/viewer/shares/{share['id']}/media?revision=1"
    response = h.client.get(route, headers={"Range": "bytes=0-3"})
    assert response.status_code == 409
    with h.repo.factory() as db:
        row = db.get(Share, share["id"])
        assert row.state == "restricted" and row.restricted_reason["code"] == "SNAPSHOT_CHANGED"
    h.repo.path(path).write_bytes(original)
    assert h.client.get(route).status_code == 409


def test_stale_media_revision_still_detects_and_persists_content_drift(h):
    version, path = saved_version(h)
    share, _ = create(h, version)
    viewer(h, share)
    assert h.client.post(BASE + f"/shares/{share['id']}/update", headers=h.owner,
                         json=update_body(version)).status_code == 200
    h.repo.path(path).write_bytes(b"new drift")
    response = h.client.get(BASE + f"/viewer/shares/{share['id']}/media?revision=1")
    assert response.status_code == 409 and response.json()["error"]["code"] == "SHARE_RESTRICTED"
    with h.repo.factory() as db:
        assert db.get(Share, share["id"]).state == "restricted"


def test_viewer_cookie_hash_and_csrf_do_not_unlock_owner_or_other_share(h):
    version, _ = saved_version(h)
    share, _ = create(h, version)
    other, _ = create(h, version)
    auth = viewer(h, share)
    with h.repo.factory() as db:
        row = db.scalars(select(ViewerSession)).one()
        assert row.token_hash == service.digest(h.client.cookies.get("episode_session_viewer"))
    assert h.client.get(BASE + f"/viewer/shares/{other['id']}").status_code == 404
    assert h.client.post(BASE + f"/shares/{share['id']}/close", headers=auth,
                         json={"expected_revision": 1, "confirmed": True}).status_code == 403
    h.client.cookies.delete("episode_session")
    assert h.client.get(BASE + f"/episodes/{h.episode}").status_code == 401
    assert h.client.get(BASE + f"/assets/{h.source}/media").status_code == 401


@pytest.mark.parametrize("mutation", ["empty", "missing", "symlink", "directory"])
def test_new_share_rejects_unusable_or_uncontrolled_media(h, tmp_path, mutation):
    version, relative = saved_version(h)
    target = h.repo.path(relative)
    target.unlink()
    if mutation == "empty":
        target.write_bytes(b"")
    elif mutation == "symlink":
        outside = tmp_path / "private-other.txt"
        outside.write_bytes(b"other private content")
        target.symlink_to(outside)
    elif mutation == "directory":
        target.mkdir()
    response = h.client.post(BASE + f"/versions/{version}/shares", headers=h.owner,
        json={"label": "朋友", "expires_days": 1, "confirmed": True, "client_request_id": uid()})
    assert response.status_code == 409
    with h.repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Share)) == 0


def test_open_descriptor_cannot_be_redirected_to_another_file(tmp_path):
    root = tmp_path / "controlled"
    (root / "outputs").mkdir(parents=True)
    name = uid() + ".mp4"
    output = root / "outputs" / name
    output.write_bytes(b"authorized")
    other = tmp_path / "unrelated"
    other.write_bytes(b"private")
    handle = service.open_media(SimpleNamespace(data_dir=root), "outputs/" + name)
    try:
        output.unlink()
        output.symlink_to(other)
        assert os.read(handle.fd, 100) == b"authorized"
        with pytest.raises(DomainError) as error:
            service.open_media(SimpleNamespace(data_dir=root), "outputs/" + name)
        assert error.value.code == "SHARE_MEDIA_UNAVAILABLE"
        assert other.read_bytes() == b"private"
    finally:
        handle.close()


def test_output_directory_symlink_and_fifo_are_rejected(tmp_path):
    root = tmp_path / "controlled"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    name = uid() + ".mp4"
    (other / name).write_bytes(b"private")
    (root / "outputs").symlink_to(other, target_is_directory=True)
    with pytest.raises(DomainError):
        service.open_media(SimpleNamespace(data_dir=root), "outputs/" + name)
    (root / "outputs").unlink()
    (root / "outputs").mkdir()
    os.mkfifo(root / "outputs" / name)
    with pytest.raises(DomainError):
        service.open_media(SimpleNamespace(data_dir=root), "outputs/" + name)


@pytest.mark.parametrize("value,expected", [(None, (0, 9, 200)), ("bytes=2-4", (2, 4, 206)),
    ("bytes=3-", (3, 9, 206)), ("bytes=-3", (7, 9, 206)), ("bytes=0-99", (0, 9, 206))])
def test_valid_single_ranges(value, expected):
    assert service.byte_range(value, 10) == expected


@pytest.mark.parametrize("value", ["bytes=0-1,4-5", "bytes=-0", "bytes=10-", "bytes=5-2", "bytes=-", "bytes=" + "9" * 5000 + "-"])
def test_invalid_ranges_are_bounded_structured_errors(value):
    with pytest.raises(DomainError) as error:
        service.byte_range(value, 10)
    assert error.value.status == 416 and error.value.code == "INVALID_RANGE"


def test_invalid_range_response_is_json_and_head_has_no_body(h):
    version, _ = saved_version(h)
    share, _ = create(h, version)
    viewer(h, share)
    route = BASE + f"/viewer/shares/{share['id']}/media?revision=1"
    for method in ("GET", "HEAD"):
        response = h.client.request(method, route, headers={"Range": "bytes=0-1,3-4"})
        assert response.status_code == 416
        assert response.headers["content-type"].startswith("application/json")
        assert "etag" not in response.headers and "last-modified" not in response.headers
        if method == "HEAD":
            assert response.content == b""
        else:
            assert response.json()["error"]["code"] == "INVALID_RANGE"


def test_stream_stops_reading_after_durable_close(h):
    content = b"a" * 150000
    version, path = saved_version(h, content=content)
    share, _ = create(h, version)
    handle = service.open_media(h.repo, path)
    chunks = service.media_chunks(h.repo, share["id"], 1, handle, 0, len(content) - 1)
    assert len(next(chunks)) == 64 * 1024
    with h.repo.transaction() as db:
        db.get(Share, share["id"]).state = "closed"
    assert list(chunks) == [] and handle.fd == -1
