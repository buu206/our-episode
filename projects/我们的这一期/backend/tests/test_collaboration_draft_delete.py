"""Explicit deletion of a holder's never-submitted local drafts only."""
from pathlib import Path

import pytest
from sqlalchemy import select

from test_collaboration import BASE, h
from app.collaboration_models import Participant, Submission
from app.models import Job, uid
from app.services import artifacts, collaboration
from app.services.artifacts import record_artifact


def private_draft(h, *, preview=True):
    auth = h.participant(h.invitation())
    response = h.upload(auth)
    assert response.status_code == 201
    row = response.json()
    if preview:
        h.run()
    return auth, row


def own_row(h, row_id):
    return next(row for row in h.client.get(BASE + "/participant/submissions").json()["items"] if row["id"] == row_id)


def test_deleting_private_draft_requires_explicit_confirmation(h):
    auth, row = private_draft(h)
    response = h.client.delete(BASE + f"/participant/submissions/{row['id']}", headers=auth)
    assert response.status_code == 409 and response.json()["error"]["code"] == "CONFIRM_REQUIRED"
    assert own_row(h, row["id"])["deleted"] is False
    assert h.client.get(BASE + f"/participant/submissions/{row['id']}/media").status_code == 200


def test_another_participant_cannot_delete_a_private_draft(h):
    _, row = private_draft(h)
    other = h.participant(h.invitation(), "另一位")
    response = h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=other)
    assert response.status_code == 404
    with h.repo.factory() as db:
        stored = db.get(Submission, row["id"])
        assert stored.state == "draft" and stored.deleted_at is None
        assert h.repo.path(stored.source_path).exists()


def test_delete_removes_original_and_preview_releases_quota_and_never_exposes_draft(h, tmp_path):
    auth, row = private_draft(h)
    outside = tmp_path / "device-original.wav"
    outside.write_bytes(b"not-an-application-copy")
    with h.repo.factory() as db:
        stored = db.get(Submission, row["id"])
        original, preview = h.repo.path(stored.source_path), h.repo.path(stored.preview_path)
        before = collaboration.shared_quota(db, h.episode)
    response = h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth)
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert not original.exists() and not preview.exists()
    assert outside.read_bytes() == b"not-an-application-copy"
    deleted = own_row(h, row["id"])
    assert deleted["deleted"] and not deleted["deletion_pending"] and not deleted["can_delete_draft"]
    assert deleted["text"] == "" and deleted["media_url"] is None
    assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"] == []
    assert h.client.get(BASE + f"/collaboration/submissions/{row['id']}/media").status_code == 404
    assert h.client.get(BASE + f"/participant/submissions/{row['id']}/media").status_code == 409
    with h.repo.factory() as db:
        after = collaboration.shared_quota(db, h.episode)
        assert after["count"] == before["count"] - 1
        assert after["bytes"] == 14 and after["seconds"] == 60  # Only the owner's source remains.
    assert h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth).status_code == 200


@pytest.mark.parametrize("state", ["submitted", "withdrawn", "adopted"])
def test_submitted_or_adopted_content_cannot_use_private_draft_delete(h, state):
    _, auth, submitted = h.submitted()
    if state == "withdrawn":
        h.client.post(BASE + f"/participant/submissions/{submitted['id']}/withdraw", headers=auth, json={"confirmed": True})
    elif state == "adopted":
        h.adopt(submitted)
    with h.repo.factory() as db:
        raw = h.repo.path(db.get(Submission, submitted["id"]).source_path)
    response = h.client.delete(BASE + f"/participant/submissions/{submitted['id']}?confirm=true", headers=auth)
    assert response.status_code == 409 and response.json()["error"]["code"] == "SUBMITTED_DELETE_FORBIDDEN"
    assert raw.exists() and own_row(h, submitted["id"])["can_delete_draft"] is False


def test_withdrawn_unsubmitted_draft_can_be_physically_deleted_only_after_separate_confirmation(h):
    auth, row = private_draft(h, preview=False)
    with h.repo.factory() as db:
        original = h.repo.path(db.get(Submission, row["id"]).source_path)
    withdrawn = h.client.post(BASE + f"/participant/submissions/{row['id']}/withdraw", headers=auth, json={"confirmed": True})
    assert withdrawn.status_code == 200 and original.exists()
    assert withdrawn.json()["can_delete_draft"] and not withdrawn.json()["deleted"]
    assert h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth).status_code == 200
    assert not original.exists()
    with h.repo.factory() as db:
        assert db.get(Job, row["preview_job_id"]).state == "failed"


def test_failed_upload_record_can_be_explicitly_deleted_without_media(h, monkeypatch):
    auth = h.participant(h.invitation())
    monkeypatch.setattr(collaboration, "MAX_INTERVIEW_BYTES", 1)
    response = h.upload(auth)
    assert response.status_code == 413
    row = h.client.get(BASE + "/participant/submissions").json()["items"][0]
    assert row["state"] == "failed" and row["can_delete_draft"] and not row["deleted"]
    response = h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth)
    assert response.status_code == 200
    assert own_row(h, row["id"])["deleted"]


def test_failed_unlink_keeps_access_revoked_and_quota_until_retry_succeeds(h, monkeypatch):
    auth, row = private_draft(h)
    with h.repo.factory() as db:
        original = h.repo.path(db.get(Submission, row["id"]).source_path)
    unlink = Path.unlink
    def locked(path, *args, **kwargs):
        if path == original:
            raise PermissionError("offline locked original")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        response = h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth)
    assert response.status_code == 503 and response.json()["error"]["code"] == "DELETION_PENDING"
    record = own_row(h, row["id"])
    assert record["deleted"] and record["deletion_pending"] and record["can_delete_draft"]
    assert record["media_url"] is None and original.exists()
    assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"] == []
    with h.repo.factory() as db:
        assert collaboration.shared_quota(db, h.episode)["count"] == 2
    assert h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth).status_code == 200
    assert not original.exists() and not own_row(h, row["id"])["deletion_pending"]


def test_delete_cleans_only_registered_preview_scratch_and_retries_failed_directory_cleanup(h, monkeypatch):
    auth, row = private_draft(h, preview=False)
    job_id = row["preview_job_id"]
    workspace = "incoming/work-" + uid()
    output = "outputs/" + uid() + ".mp4"
    record_artifact(h.repo, job_id, workspace, "directory")
    record_artifact(h.repo, job_id, output)
    directory = h.repo.path(workspace)
    directory.mkdir()
    (directory / "private.wav").write_bytes(b"partial")
    h.repo.path(output).write_bytes(b"partial-preview")
    unrelated = h.repo.path("incoming/work-" + uid())
    unrelated.mkdir()
    (unrelated / "unregistered.wav").write_bytes(b"not-owned")
    with h.repo.transaction() as db:
        db.get(Job, job_id).state = "running"
    rmtree = artifacts.shutil.rmtree
    def locked(path, *args, **kwargs):
        if path == directory:
            raise PermissionError("offline locked workspace")
        return rmtree(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.shutil, "rmtree", locked)
        response = h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth)
    assert response.status_code == 503 and directory.exists()
    assert not h.repo.path(output).exists() and unrelated.exists()
    assert own_row(h, row["id"])["deletion_pending"]
    assert h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth).status_code == 200
    assert not directory.exists() and unrelated.exists()
    with h.repo.factory() as db:
        job = db.get(Job, job_id)
        assert job.state == "failed" and not job.payload["artifacts"]


def test_private_delete_does_not_touch_another_participants_pending_cleanup(h, monkeypatch):
    auth, row = private_draft(h)
    other_invitation = h.invitation()
    other_relative = "collaboration/" + uid() + ".wav"
    other_path = h.repo.path(other_relative)
    other_path.write_bytes(b"another-private-draft")
    with h.repo.transaction() as db:
        other_person = Participant(invite_id=other_invitation["id"], display_name="另一位")
        db.add(other_person)
        db.flush()
        other_row = Submission(owner_id="alice", episode_id=h.episode, invite_id=other_invitation["id"],
                               participant_id=other_person.id, client_request_id=uid(), kind="audio", state="withdrawn",
                               deleted_at=1, source_path=other_relative, size=other_path.stat().st_size, duration=1)
        db.add(other_row)
        db.flush()
        other_id = other_row.id
    unlink = Path.unlink
    def must_not_touch(path, *args, **kwargs):
        assert path != other_path, "a participant deletion touched another holder's original"
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", must_not_touch)
        response = h.client.delete(BASE + f"/participant/submissions/{row['id']}?confirm=true", headers=auth)
    assert response.status_code == 200 and other_path.exists()
    with h.repo.factory() as db:
        assert db.get(Submission, other_id).source_path == other_relative
