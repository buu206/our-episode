"""Backward-compatible frontend facts over isolated, persistent local storage."""
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.collaboration_models import Adoption, Invitation, Submission
from app.models import Asset, Episode, Job, Version, uid
from app.services.artifacts import record_artifact
from app.services.collaboration import shared_quota
from test_collaboration import BASE, h


def episode(h):
    response = h.client.get(BASE + f"/episodes/{h.episode}")
    assert response.status_code == 200
    return response.json()


def delete(h, asset_id=None):
    return h.client.delete(BASE + f"/assets/{asset_id or h.source}?confirm=true", headers=h.owner)


def block_file(monkeypatch, target):
    unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == target:
            raise PermissionError("isolated test cleanup failure")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", locked)


def published(h, *, asset_id=None, narration=False):
    output = "outputs/" + uid() + (".wav" if narration else ".mp4")
    h.repo.path(output).write_bytes(b"offline-derived-copy")
    with h.repo.transaction() as db:
        version = Version(owner_id="alice", episode_id=h.episode, title="待删除衍生内容",
            state="confirmed", confirmed_at="offline-confirmed",
            output_path=None if narration else output, narration_path=output if narration else None,
            segments=[{"asset_id": asset_id or h.source, "start": 0, "end": 1, "caption": ""}])
        db.add(version)
    return h.repo.path(output)


@pytest.mark.parametrize('kind', ['asset', 'media', 'export'])
def test_head_media_uses_the_same_access_checks_and_range_headers(h, kind):
    if kind == 'asset':
        url = BASE + f'/assets/{h.source}/media'
    else:
        published(h)
        with h.repo.factory() as db:
            version_id = db.scalars(select(Version)).one().id
        url = BASE + f'/versions/{version_id}/{kind}'
    full = h.client.get(url)
    assert full.status_code == 200
    head = h.client.head(url)
    assert head.status_code == 200 and head.content == b''
    assert head.headers['content-length'] == full.headers['content-length']
    ranged = h.client.head(url, headers={'Range': 'bytes=0-2'})
    assert ranged.status_code == 206 and ranged.content == b''
    assert ranged.headers['content-range'].startswith('bytes 0-2/')
    assert delete(h).status_code == 200
    assert h.client.head(url).status_code in (404, 409)
    h.client.post(BASE + '/auth/login', json={'invite_code': 'test-other'})
    assert h.client.head(url).status_code == 404
    h.client.cookies.clear()
    assert h.client.head(url).status_code == 401


def test_quota_is_aggregate_of_originals_and_private_drafts_without_private_metadata(h):
    auth = h.participant(h.invitation("contribute"))
    draft = h.upload(auth, kind="video", text="private-unsubmitted-text")
    assert draft.status_code == 201
    with h.repo.transaction() as db:
        row = db.get(Submission, draft.json()["id"])
        row.filename = "private-unsubmitted-filename.mp4"
    result = episode(h)
    assert result["quota"] == {"original_count": 2, "total_seconds": 63.0, "total_bytes": 27,
        "max_count": 8, "max_seconds": 600.0, "max_bytes": 1073741824}
    assert result["pending_cleanup"] == []
    assert len(result["assets"]) == 1
    serialized = json.dumps(result)
    assert "private-unsubmitted-text" not in serialized
    assert "private-unsubmitted-filename" not in serialized
    assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"] == []
    listing = h.client.get(BASE + "/episodes").json()["items"][0]
    assert listing["quota"] == result["quota"]
    withdrawn = h.client.post(BASE + f"/participant/submissions/{draft.json()['id']}/withdraw",
        headers=auth, json={"confirmed": True})
    assert withdrawn.status_code == 200
    assert episode(h)["quota"] == result["quota"]


def test_adopted_copy_is_not_counted_again_and_pending_private_delete_stays_private(h, monkeypatch):
    _, _, submitted = h.submitted()
    h.adopt(submitted)
    result = episode(h)
    assert len(result["assets"]) == 2 and result["quota"]["original_count"] == 2
    auth = h.participant(h.invitation())
    draft = h.upload(auth).json()
    with h.repo.factory() as db:
        raw = h.repo.path(db.get(Submission, draft["id"]).source_path)
    with monkeypatch.context() as patch:
        block_file(patch, raw)
        response = h.client.delete(BASE + f"/participant/submissions/{draft['id']}?confirm=true", headers=auth)
    assert response.status_code == 503
    result = episode(h)
    assert result["quota"]["original_count"] == 3
    assert result["pending_cleanup"] == []  # No owner Asset was ever created for the private draft.
    assert h.client.delete(BASE + f"/participant/submissions/{draft['id']}?confirm=true", headers=auth).status_code == 200
    assert episode(h)["quota"]["original_count"] == 2


@pytest.mark.parametrize("remaining", ["source", "output", "narration", "checkpoint", "context"])
def test_pending_cleanup_tracks_each_remaining_reference_and_refresh_retry(h, monkeypatch, remaining):
    with h.repo.factory() as db:
        raw = h.repo.path(db.get(Asset, h.source).path)
    if remaining == "source":
        target = raw
    elif remaining in ("output", "narration"):
        target = published(h, narration=remaining == "narration")
    elif remaining == "context":
        invitation = h.invitation("review")
        h.run()
        with h.repo.factory() as db:
            target = h.repo.path(db.get(Invitation, invitation["id"]).context_path)
    else:
        relative = "outputs/" + uid() + ".wav"
        target = h.repo.path(relative)
        target.write_bytes(b"paid-checkpoint-copy")
        with h.repo.transaction() as db:
            db.add(Job(owner_id="alice", episode_id=h.episode, kind="narration", state="failed",
                idempotency_key=uid(), payload={"asset_ids": [h.source], "steps": {"tts": {"path": str(target)}}}))
    with monkeypatch.context() as patch:
        block_file(patch, target)
        response = delete(h)
        assert response.status_code == 503 and response.json()["error"]["code"] == "DELETION_PENDING"
        first = episode(h)
        assert first["assets"] == []
        assert first["pending_cleanup"] == [{"asset_id": h.source, "filename": "scene.mp4"}]
        # A fresh browser request reconstructs the entry from persisted state.
        with TestClient(h.client.app) as refreshed:
            refreshed.cookies.update(h.client.cookies)
            saved = refreshed.get(BASE + f"/episodes/{h.episode}")
            assert saved.status_code == 200
            assert saved.json()["pending_cleanup"] == first["pending_cleanup"]
        assert target.exists()
        assert h.client.get(BASE + f"/assets/{h.source}/media").status_code == 404
    assert delete(h).status_code == 200
    assert episode(h)["pending_cleanup"] == []
    assert not target.exists()


def test_context_job_orphan_is_pending_and_retry_cleans_only_registered_paths(h, monkeypatch):
    invitation = h.invitation("review")
    with h.repo.transaction() as db:
        job_id = db.get(Invitation, invitation["id"]).context_job_id
        db.get(Job, job_id).state = "running"
    relative = "outputs/" + uid() + ".mp4"
    record_artifact(h.repo, job_id, relative)
    target = h.repo.path(relative)
    target.write_bytes(b"unpublished-context")
    unrelated = h.repo.path("outputs/" + uid() + ".mp4")
    unrelated.write_bytes(b"unmarked-output")
    with monkeypatch.context() as patch:
        block_file(patch, target)
        assert delete(h).status_code == 503
        assert episode(h)["pending_cleanup"][0]["asset_id"] == h.source
    with h.repo.factory() as db:
        assert db.get(Job, job_id).state == "failed"
    assert delete(h).status_code == 200
    assert not target.exists() and unrelated.read_bytes() == b"unmarked-output"
    assert episode(h)["pending_cleanup"] == []


def test_registered_workspace_and_invalid_reference_never_claim_success(h, monkeypatch, tmp_path):
    with h.repo.transaction() as db:
        task = Job(owner_id="alice", episode_id=h.episode, kind="analysis", state="failed",
                   idempotency_key=uid(), payload={"asset_ids": [h.source]})
        db.add(task)
        db.flush()
        job_id = task.id
    relative = "incoming/work-" + uid()
    record_artifact(h.repo, job_id, relative, "directory")
    workspace = h.repo.path(relative)
    workspace.mkdir()
    (workspace / "local.wav").write_bytes(b"work-copy")
    from app.services import artifacts
    rmtree = artifacts.shutil.rmtree
    def fail_workspace(path, *args, **kwargs):
        if path == workspace:
            raise PermissionError("test workspace locked")
        return rmtree(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.shutil, "rmtree", fail_workspace)
        assert delete(h).status_code == 503
        assert episode(h)["pending_cleanup"]
    assert delete(h).status_code == 200 and not workspace.exists()
    outside = tmp_path / "device-original.wav"
    outside.write_bytes(b"user-source-untouched")
    with h.repo.transaction() as db:
        job = db.get(Job, job_id)
        job.payload = {**job.payload, "steps": {"tts": {"path": str(outside)}}}
    assert delete(h).status_code == 503
    assert episode(h)["pending_cleanup"]
    assert outside.read_bytes() == b"user-source-untouched"


def test_adopted_original_still_pending_after_adopted_asset_copy_is_removed(h, monkeypatch):
    _, _, submitted = h.submitted()
    asset_id = h.adopt(submitted)
    with h.repo.factory() as db:
        raw = h.repo.path(db.get(Submission, submitted["id"]).source_path)
        adopted = db.get(Asset, asset_id)
        copied, name = h.repo.path(adopted.path), adopted.filename
    with monkeypatch.context() as patch:
        block_file(patch, raw)
        assert delete(h, asset_id).status_code == 503
        saved = episode(h)
        assert saved["pending_cleanup"] == [{"asset_id": asset_id, "filename": name}]
        assert saved["quota"]["original_count"] == 2
        assert not copied.exists() and raw.exists()
    assert delete(h, asset_id).status_code == 200
    assert episode(h)["pending_cleanup"] == []
    assert episode(h)["quota"]["original_count"] == 1


def test_same_submission_sibling_output_is_part_of_original_delete_scope(h, monkeypatch):
    _, auth, submission = h.submitted()
    first = h.adopt(submission)
    revision = h.client.post(BASE + f"/collaboration/submissions/{submission['id']}/revisions", headers=h.owner,
        json={"text": "本人已确认的新版本", "start": .5, "end": 2}).json()
    assert h.client.post(BASE + f"/participant/revisions/{revision['id']}/decision", headers=auth,
        json={"decision": "confirmed"}).status_code == 200
    job = h.client.post(BASE + f"/collaboration/revisions/{revision['id']}/adopt", headers=h.owner, json={}).json()
    h.run()
    with h.repo.factory() as db:
        second = db.get(Job, job["id"]).result["asset_id"]
    target = published(h, asset_id=second)
    with monkeypatch.context() as patch:
        block_file(patch, target)
        assert delete(h, first).status_code == 503
        pending = {row["asset_id"] for row in episode(h)["pending_cleanup"]}
        assert pending == {first, second}
    assert delete(h, first).status_code == 200
    assert episode(h)["pending_cleanup"] == []


def test_status_get_is_read_only_and_never_leaks_other_owner_or_unmarked_files(h, monkeypatch):
    other_raw = "assets/" + uid() + ".mp4"
    h.repo.path(other_raw).write_bytes(b"another-owner-copy")
    collab_raw = "collaboration/" + uid() + ".wav"
    h.repo.path("collaboration").mkdir(exist_ok=True)
    h.repo.path(collab_raw).write_bytes(b"another-owner-private")
    with h.repo.transaction() as db:
        other_episode = Episode(owner_id="bob", title="private-owner-title")
        db.add(other_episode)
        db.flush()
        other = Asset(owner_id="bob", episode_id=other_episode.id, path=other_raw,
            filename="private-other-filename.mp4", deleted=True, size=10, duration=3,
            width=1280, height=720, has_audio=True)
        db.add(other)
        db.add(Submission(owner_id="bob", episode_id=other_episode.id, participant_id=uid(), invite_id=uid(),
            client_request_id=uid(), kind="audio", source_path=collab_raw, state="withdrawn", deleted_at=time.time()))
        db.flush()
        other_id, other_episode_id = other.id, other_episode.id
        db.get(Asset, h.source).deleted = True
    def no_unlink(*args, **kwargs):
        raise AssertionError("GET must not clean files")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", no_unlink)
        saved = episode(h)
    assert saved["pending_cleanup"] == [{"asset_id": h.source, "filename": "scene.mp4"}]
    assert "private-other-filename" not in json.dumps(saved)
    assert h.client.get(BASE + f"/episodes/{other_episode_id}").status_code == 404
    assert delete(h, other_id).status_code == 404
    assert delete(h).status_code == 200  # Unrelated pending work does not cause false 503.
    assert h.repo.path(other_raw).read_bytes() == b"another-owner-copy"
    assert h.repo.path(collab_raw).read_bytes() == b"another-owner-private"


def reviewed_body(h, *, count=1, note="已核对内容"):
    items = [{"id": uid(), "asset_id": h.source, "start": 0, "end": 1, "note": note, "kind": "visual"}
             for _ in range(count)]
    with h.repo.transaction() as db:
        row = db.get(Episode, h.episode)
        row.evidence_revision, row.analysis_state, row.analysis_asset_ids = 7, "review", [h.source]
        row.evidence = items
    return {"revision": 7, "items": items, "confirmed": True}


def put_body(h, body, *, chunked=False):
    content = (body[i:i + 60000] for i in range(0, len(body), 60000)) if chunked else body
    return h.client.put(BASE + f"/episodes/{h.episode}/evidence", content=content,
                        headers={**h.owner, "Content-Type": "application/json"})


def test_all_one_hundred_max_length_unicode_evidence_items_can_be_saved(h):
    body = reviewed_body(h, count=100, note="🙂" * 2000)
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert 65536 < len(encoded) < 2 * 1024 * 1024
    response = put_body(h, encoded)
    assert response.status_code == 200
    assert len(response.json()["evidence"]) == 100
    assert response.json()["evidence"][0]["note"] == "🙂" * 2000


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("excess", [0, 1])
def test_evidence_two_mib_boundary_for_declared_and_streamed_bodies(h, chunked, excess):
    body = json.dumps(reviewed_body(h), ensure_ascii=False).encode("utf-8")
    padded = body + b" " * (2 * 1024 * 1024 + excess - len(body))
    response = put_body(h, padded, chunked=chunked)
    assert response.status_code == (413 if excess else 200)
    if excess:
        assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
        assert episode(h)["analysis_state"] == "review"


@pytest.mark.parametrize("method,path", [("POST", "/episodes"), ("POST", "/auth/login"),
    ("POST", "/episodes/any/evidence"), ("PUT", "/episodes/any/evidence/"),
    ("PUT", "/episodes/any/nested/evidence"), ("PUT", "/episodes/any/evidence-extra")])
def test_larger_allowance_cannot_escape_exact_evidence_put_route(h, method, path):
    response = h.client.request(method, BASE + path, headers={**h.owner, "Content-Type": "application/json"},
                                content=b"{}" + b" " * 65535)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"


@pytest.mark.parametrize("invalid", ["note", "count", "range", "unknown", "revision"])
def test_evidence_schema_identity_bounds_and_revision_are_not_relaxed(h, invalid):
    body = reviewed_body(h)
    if invalid == "note":
        body["items"][0]["note"] = "字" * 2001
    elif invalid == "count":
        body["items"] *= 101
    elif invalid == "range":
        body["items"][0]["end"] = 61
    elif invalid == "unknown":
        body["items"][0]["id"] = uid()
    else:
        body["revision"] = 6
    encoded = json.dumps(body, ensure_ascii=False).encode() + b" " * 65536
    response = put_body(h, encoded)
    assert response.status_code == (409 if invalid == "revision" else 422)
    assert episode(h)["analysis_state"] == "review"


def test_new_episode_has_zero_quota_and_status_stage_is_current(h):
    response = h.client.post(BASE + "/episodes", headers=h.owner, json={"title": "空白一期"})
    assert response.status_code == 201
    assert response.json()["quota"] == {"original_count": 0, "total_seconds": 0.0, "total_bytes": 0,
        "max_count": 8, "max_seconds": 600.0, "max_bytes": 1073741824}
    assert response.json()["pending_cleanup"] == []
    assert h.client.get(BASE + "/status").json()["stage"] == "阶段3-1"
