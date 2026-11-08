"""Interrupted publication and deletion boundaries, with isolated process exit."""
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from test_collaboration import BASE, h
from app.collaboration_models import Adoption, Invitation, Submission
from app.models import Asset, Job, Version, uid
from app.services.artifacts import recover_artifacts
from app.services.collaboration import cleanup_collaboration, shared_quota
from app.worker import Worker


def test_death_between_hard_link_and_database_commit_recovers_only_registered_files(h):
    _, _, submission = h.submitted()
    revision_id = submission["revisions"][0]["id"]
    response = h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={})
    job_id = response.json()["id"]
    script = """
import os, sys
from pathlib import Path
from app.repository import Repository
from app.worker import Worker
from app.services import interview_media
def offline_render(source, kind, output, label, **kwargs):
    output.write_bytes(b'offline-final-but-not-committed')
    return {'duration': 3.0, 'width': 1280, 'height': 720, 'has_audio': True}
interview_media.materialize_interview = offline_render
original = os.link
def abrupt_link(source, destination, *args, **kwargs):
    original(source, destination, *args, **kwargs)
    if Path(destination).parent.name == 'assets':
        os._exit(19)
os.link = abrupt_link
repo = Repository(Path(sys.argv[1]))
Worker(repo).process_one()
"""
    result = subprocess.run([sys.executable, "-c", script, str(h.repo.data_dir)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
    assert result.returncode == 19
    with h.repo.factory() as db:
        job = db.get(Job, job_id)
        assert job.state == "running"
        adoption = db.scalars(select(Adoption).where(Adoption.revision_id == revision_id)).one()
        orphan = h.repo.path(adoption.output_path)
        assert orphan.exists() and adoption.asset_id is None
        raw = h.repo.path(db.get(Submission, submission["id"]).source_path)
        assert db.scalar(select(func.count()).select_from(Asset)) == 1
    h.repo.recover()
    assert recover_artifacts(h.repo).pending == 0
    assert cleanup_collaboration(h.repo).pending == 0
    assert not orphan.exists() and raw.exists()
    assert list(h.repo.path("incoming").iterdir()) == []
    # Retry the same confirmed adoption, without a second submission/revision.
    response = h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={})
    assert response.json()["id"] == job_id and response.json()["state"] == "queued"
    h.run()
    with h.repo.factory() as db:
        assert db.get(Job, job_id).state == "succeeded"
        assert db.scalar(select(func.count()).select_from(Adoption)) == 1


def test_unfinished_upload_manifest_cleans_after_recovery_but_preserves_other_sources(h):
    invitation = h.invitation()
    auth = h.participant(invitation)
    identity = h.client.get(BASE + "/participant/auth/me").json()
    relative = "collaboration/" + uid() + ".wav"
    h.repo.path("collaboration").mkdir(exist_ok=True)
    raw = h.repo.path(relative)
    raw.write_bytes(b"interrupted-copy")
    unrelated = h.repo.path("collaboration/" + uid() + ".wav")
    unrelated.write_bytes(b"must-not-be-swept")
    with h.repo.transaction() as db:
        job = Job(owner_id="alice", episode_id=h.episode, kind="collaboration_upload", state="running",
                  idempotency_key=uid())
        db.add(job)
        db.flush()
        row = Submission(owner_id="alice", episode_id=h.episode, invite_id=invitation["id"],
                         participant_id=identity["participant_id"], kind="audio", state="uploading",
                         client_request_id=uid(), source_path=relative, upload_job_id=job.id, size=16)
        db.add(row)
        db.flush()
        row_id = row.id
    h.repo.recover()
    assert cleanup_collaboration(h.repo).pending == 0
    assert not raw.exists() and unrelated.exists()
    with h.repo.factory() as db:
        row = db.get(Submission, row_id)
        assert row.state == "failed" and row.source_path is None


def test_failed_source_unlink_remains_in_quota_until_physical_cleanup_succeeds(h, monkeypatch):
    _, _, submission = h.submitted()
    asset_id = h.adopt(submission)
    with h.repo.transaction() as db:
        row = db.get(Submission, submission["id"])
        raw = h.repo.path(row.source_path)
        before = shared_quota(db, h.episode)
        db.get(Asset, asset_id).deleted = True
    unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == raw:
            raise PermissionError("offline locked file")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        assert cleanup_collaboration(h.repo).pending == 1
    with h.repo.factory() as db:
        assert shared_quota(db, h.episode) == before
    assert raw.exists()
    assert cleanup_collaboration(h.repo).pending == 0
    assert not raw.exists()
    with h.repo.factory() as db:
        assert shared_quota(db, h.episode)["count"] == before["count"] - 1


def test_context_failure_can_retry_without_claiming_access_to_a_deleted_source(h):
    invitation = h.invitation()
    with h.repo.transaction() as db:
        task = db.get(Job, invitation["context_job_id"])
        task.state, task.error = "failed", {"code": "OFFLINE_FAILURE", "message": "测试失败"}
    response = h.client.post(BASE + f"/collaboration/invitations/{invitation['id']}/retry", headers=h.owner, json={})
    assert response.status_code == 200 and response.json()["state"] == "queued"
    h.run()
    with h.repo.transaction() as db:
        db.get(Asset, h.source).deleted = True
    assert cleanup_collaboration(h.repo).pending == 0
    response = h.client.post(BASE + f"/collaboration/invitations/{invitation['id']}/retry", headers=h.owner, json={})
    assert response.status_code == 409


def test_corrupt_manifest_cannot_delete_outside_product_directory(h, tmp_path):
    _, _, submission = h.submitted()
    outside = tmp_path / "user-original.wav"
    outside.write_bytes(b"never-delete")
    with h.repo.transaction() as db:
        row = db.get(Submission, submission["id"])
        row.source_path = str(outside)
        row.deleted_at = 1
    assert cleanup_collaboration(h.repo).pending == 1
    assert outside.read_bytes() == b"never-delete"


def test_deleting_one_adopted_revision_cleans_all_outputs_from_same_deleted_original(h):
    _, auth, submission = h.submitted()
    first = h.adopt(submission)
    revision = h.client.post(BASE + f"/collaboration/submissions/{submission['id']}/revisions", headers=h.owner,
                             json={"text": "第二个本人确认修订", "start": .5, "end": 2}).json()
    h.client.post(BASE + f"/participant/revisions/{revision['id']}/decision", headers=auth, json={"decision": "confirmed"})
    response = h.client.post(BASE + f"/collaboration/revisions/{revision['id']}/adopt", headers=h.owner, json={})
    h.run()
    versions, outputs = [], []
    with h.repo.transaction() as db:
        second = db.get(Job, response.json()["id"]).result["asset_id"]
        for asset_id in (first, second):
            asset = db.get(Asset, asset_id)
            relative = "outputs/" + uid() + ".mp4"
            output = h.repo.path(relative)
            output.write_bytes(b"already-published-offline-video")
            outputs.append(output)
            version = Version(owner_id="alice", episode_id=h.episode, title="已使用这个投稿", state="confirmed",
                              confirmed_at="before", output_path=relative,
                              segments=[{"asset_id": asset_id, "start": 0, "end": asset.duration, "caption": ""}])
            db.add(version)
            db.flush()
            versions.append(version.id)
    response = h.client.delete(BASE + f"/assets/{first}?confirm=true", headers=h.owner)
    assert response.status_code == 200, response.text
    with h.repo.factory() as db:
        assert db.get(Asset, first).deleted and db.get(Asset, second).deleted
        for version_id in versions:
            version = db.get(Version, version_id)
            assert version.state == "restricted" and version.confirmed_at is None
            assert version.error["code"] == "SOURCE_REMOVED"
    assert all(not output.exists() for output in outputs)


def test_http_delete_reports_pending_when_collaboration_original_cannot_be_removed(h, monkeypatch):
    _, _, submission = h.submitted()
    asset_id = h.adopt(submission)
    with h.repo.factory() as db:
        raw = h.repo.path(db.get(Submission, submission["id"]).source_path)
    unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == raw:
            raise PermissionError("offline locked original")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        response = h.client.delete(BASE + f"/assets/{asset_id}?confirm=true", headers=h.owner)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DELETION_PENDING"
    assert raw.exists()


def test_worker_start_cleans_interrupted_upload_after_marking_job_failed(h):
    invitation = h.invitation("contribute")
    h.participant(invitation)
    identity = h.client.get(BASE + "/participant/auth/me").json()
    h.repo.path("collaboration").mkdir(exist_ok=True)
    relative = "collaboration/" + uid() + ".wav"
    raw = h.repo.path(relative)
    raw.write_bytes(b"private-unfinished-source")
    with h.repo.transaction() as db:
        task = Job(owner_id="alice", episode_id=h.episode, kind="collaboration_upload", state="running", idempotency_key=uid())
        db.add(task)
        db.flush()
        row = Submission(owner_id="alice", episode_id=h.episode, invite_id=invitation["id"],
                         participant_id=identity["participant_id"], client_request_id=uid(), kind="audio", state="uploading",
                         source_path=relative, upload_job_id=task.id, size=raw.stat().st_size)
        db.add(row)
        db.flush()
        row_id = row.id
    worker = Worker(h.repo)
    try:
        worker.start()
    finally:
        worker.close()
    with h.repo.factory() as db:
        row = db.get(Submission, row_id)
        assert row.state == "failed" and row.source_path is None
    assert not raw.exists()
