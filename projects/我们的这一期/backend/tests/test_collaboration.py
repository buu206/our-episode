"""Permission/state tests use explicitly offline media helpers, never a model."""
from dataclasses import dataclass, field
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.collaboration_models import (Adoption, ExpressionRevision, Invitation, Objection,
                                      Participant, ParticipantSession, Submission)
from app.config import Settings
from app.models import Asset, Episode, Job, Version, uid
from app.repository import DomainError
from app.services.collaboration import (cleanup_collaboration, guard_asset, run_collaboration_job,
                                        shared_quota)
from app.worker import Worker

BASE = "/api/v1"


@dataclass
class Harness:
    client: TestClient
    owner: dict = field(repr=False)
    episode: str
    source: str

    @property
    def repo(self):
        return self.client.app.state.repo

    def invitation(self, role="interview", **overrides):
        body = {"role": role, "label": "朋友", "questions": ["当时在做什么？"],
                "context": {"asset_id": self.source, "start": 0, "end": 10} if role != "contribute" else None}
        body.update(overrides)
        response = self.client.post(BASE + f"/episodes/{self.episode}/collaboration/invitations",
                                    headers=self.owner, json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def participant(self, invite, label="小林"):
        response = self.client.post(BASE + "/participant/auth/login", json={"invite_code": invite["invite_code"], "display_name": label})
        assert response.status_code == 200, response.text
        return {"X-CSRF-Token": response.json()["csrf_token"]}

    def upload(self, auth, *, kind="audio", request_id=None, content=b"offline-audio", text="我当时在查菜谱"):
        return self.client.post(BASE + "/participant/submissions/upload", headers=auth,
            data={"kind": kind, "client_request_id": request_id or uid(), "text": text},
            files={"file": ("voice.wav" if kind == "audio" else "scene.mp4", content, "application/octet-stream")})

    def run(self):
        for _ in range(20):
            with self.repo.factory() as db:
                if not db.scalar(select(func.count()).select_from(Job).where(Job.state == "queued")):
                    return
            Worker(self.repo).process_one()
        pytest.fail("offline jobs did not finish")

    def submitted(self, role="interview"):
        invitation = self.invitation(role)
        auth = self.participant(invitation)
        response = self.upload(auth, kind="video" if role == "contribute" else "audio")
        assert response.status_code == 201, response.text
        row = response.json()
        self.run()
        response = self.client.post(BASE + f"/participant/submissions/{row['id']}/submit", headers=auth, json={"confirmed": True})
        assert response.status_code == 200, response.text
        return invitation, auth, response.json()

    def adopt(self, submission):
        revision = submission["revisions"][0]
        response = self.client.post(BASE + f"/collaboration/revisions/{revision['id']}/adopt", headers=self.owner, json={})
        assert response.status_code == 200, response.text
        self.run()
        with self.repo.factory() as db:
            job = db.get(Job, response.json()["id"])
            assert job.state == "succeeded", job.error
            return job.result["asset_id"]


@pytest.fixture
def h(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    from app.main import create_app
    from app.services import interview_media, media
    info = {"duration": 3.0, "width": 1280, "height": 720, "has_audio": True, "size": 13}
    monkeypatch.setattr(interview_media, "probe_interview", lambda *_: info.copy())
    monkeypatch.setattr(media, "probe", lambda *_: info.copy())
    def materialize(source, kind, output, label, start=0, end=None, expression=""):
        output.write_bytes(b"offline-test-video")
        return {**info, "duration": (end - start) if end is not None else 3.0}
    def clip(source, output, start, end):
        output.write_bytes(b"offline-context-only")
        return {**info, "duration": end - start}
    monkeypatch.setattr(interview_media, "materialize_interview", materialize)
    monkeypatch.setattr(interview_media, "preview_contribution", lambda source, output:
                        materialize(source, "video", output, ""))
    monkeypatch.setattr(interview_media, "context_clip", clip)
    app = create_app(Settings(data_dir=tmp_path / "isolated-collaboration", invites={"test-owner": "alice", "test-other": "bob"}, worker_enabled=False))
    with TestClient(app, raise_server_exceptions=True) as client:
        login = client.post(BASE + "/auth/login", json={"invite_code": "test-owner"})
        owner = {"X-CSRF-Token": login.json()["csrf_token"]}
        episode = client.post(BASE + "/episodes", headers=owner, json={"title": "协作测试"}).json()["id"]
        repo = app.state.repo
        relative = "assets/" + uid() + ".mp4"
        repo.path(relative).write_bytes(b"offline-source")
        with repo.transaction() as db:
            asset = Asset(owner_id="alice", episode_id=episode, filename="scene.mp4", path=relative,
                          size=14, duration=60, width=1280, height=720, has_audio=True)
            db.add(asset)
            db.flush()
            source = asset.id
        yield Harness(client, owner, episode, source)


def test_invite_code_only_returned_once_and_holder_has_separate_cookie_and_csrf(h):
    invitation = h.invitation()
    participant = h.participant(invitation)
    listed = h.client.get(BASE + f"/episodes/{h.episode}/collaboration/invitations").json()
    assert listed["items"][0]["id"] == invitation["id"]
    assert "invite_code" not in listed["items"][0]
    with h.repo.factory() as db:
        row = db.get(Invitation, invitation["id"])
        assert row.code_hash != invitation["invite_code"] and len(row.code_hash) == 64
        session = db.scalars(select(ParticipantSession)).one()
        assert session.token_hash != h.client.cookies.get("episode_session_participant")
    response = h.client.put(BASE + "/participant/invitation/questions", headers=h.owner, json={"questions": []})
    assert response.status_code == 403
    # A participant cannot use its CSRF to mutate the owner's application state.
    response = h.client.post(BASE + "/episodes", headers=participant, json={"title": "not-owner"})
    assert response.status_code == 403
    assert h.client.get(BASE + "/auth/me").json()["user_id"] == "alice"


def test_holder_without_owner_cookie_cannot_read_whole_episode_or_source(h):
    invitation = h.invitation()
    h.participant(invitation)
    h.run()
    h.client.cookies.delete("episode_session")
    assert h.client.get(BASE + f"/episodes/{h.episode}").status_code == 401
    assert h.client.get(BASE + f"/assets/{h.source}/media").status_code == 401
    response = h.client.get(BASE + "/participant/context/media")
    assert response.status_code == 200 and response.content == b"offline-context-only"


@pytest.mark.parametrize("start,end", [(-1, 3), (0, 31), (60, 61), (3, 3)])
def test_invitation_context_cannot_exceed_source_or_thirty_seconds(h, start, end):
    response = h.client.post(BASE + f"/episodes/{h.episode}/collaboration/invitations", headers=h.owner,
        json={"role": "review", "label": "核对", "context": {"asset_id": h.source, "start": start, "end": end}, "questions": []})
    assert response.status_code in (409, 422)


def test_other_owner_cannot_invite_or_read_collaboration_records(h):
    invitation = h.invitation()
    auth = h.client.post(BASE + "/auth/login", json={"invite_code": "test-other"}).json()
    other = {"X-CSRF-Token": auth["csrf_token"]}
    assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/invitations").status_code == 404
    assert h.client.post(BASE + f"/collaboration/invitations/{invitation['id']}/close", headers=other).status_code == 404


def test_closed_or_expired_invitation_cannot_be_first_claimed(h):
    invitation = h.invitation()
    h.client.post(BASE + f"/collaboration/invitations/{invitation['id']}/close", headers=h.owner)
    response = h.client.post(BASE + "/participant/auth/login", json={"invite_code": invitation["invite_code"], "display_name": "朋友"})
    assert response.status_code == 409
    second = h.invitation()
    with h.repo.transaction() as db:
        db.get(Invitation, second["id"]).expires_at = time.time() - 1
    response = h.client.post(BASE + "/participant/auth/login", json={"invite_code": second["invite_code"], "display_name": "朋友"})
    assert response.status_code == 409


def test_closing_invite_does_not_strip_existing_holders_withdrawal_right(h):
    invitation, auth, submission = h.submitted()
    h.client.post(BASE + f"/collaboration/invitations/{invitation['id']}/close", headers=h.owner)
    auth = h.participant(invitation)  # Same bearer identity can log back in.
    response = h.upload(auth)
    assert response.status_code == 409
    response = h.client.post(BASE + f"/participant/submissions/{submission['id']}/withdraw", headers=auth, json={"confirmed": True})
    assert response.status_code == 200 and response.json()["state"] == "withdrawn"
    with h.repo.factory() as db:
        assert h.repo.path(db.get(Submission, submission["id"]).source_path).exists()


def test_person_can_edit_or_skip_questions_without_blocking_other_assets(h):
    invitation = h.invitation()
    auth = h.participant(invitation)
    questions = invitation["questions"]
    questions[0].update(text="换成这个问题", skipped=True)
    response = h.client.put(BASE + "/participant/invitation/questions", headers=auth, json={"questions": questions})
    assert response.status_code == 200 and response.json()["questions"][0]["skipped"]
    with h.repo.factory() as db:
        guard_asset(db, db.get(Asset, h.source), {"start": 0, "end": 2, "caption": ""})


def test_draft_is_private_and_confirmation_waits_for_actual_preview(h):
    invitation = h.invitation()
    auth = h.participant(invitation)
    response = h.upload(auth)
    assert response.status_code == 201
    submission = response.json()
    route = BASE + f"/participant/submissions/{submission['id']}/submit"
    assert h.client.post(route, headers=auth, json={"confirmed": True}).status_code == 409
    assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"] == []
    assert h.client.get(BASE + f"/collaboration/submissions/{submission['id']}/media").status_code == 404
    h.run()
    assert h.client.get(BASE + f"/participant/submissions/{submission['id']}/media").status_code == 200
    assert h.client.post(route, headers=auth, json={"confirmed": False}).status_code == 422
    assert h.client.post(route, headers=auth, json={"confirmed": True}).status_code == 200
    assert len(h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"]) == 1


def test_abandoned_private_draft_does_not_reveal_even_its_filename_to_owner(h):
    auth = h.participant(h.invitation())
    submission = h.upload(auth).json()
    response = h.client.post(BASE + f"/participant/submissions/{submission['id']}/withdraw", headers=auth,
                             json={"confirmed": True})
    assert response.status_code == 200 and response.json()["state"] == "withdrawn"
    assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"] == []
    assert h.client.get(BASE + f"/collaboration/submissions/{submission['id']}/media").status_code == 404
    assert h.client.post(BASE + f"/collaboration/submissions/{submission['id']}/revisions", headers=h.owner,
                          json={"text": "未经提交", "start": 0, "end": 1}).status_code == 404


def test_upload_same_key_reuses_input_but_changed_content_conflicts(h):
    auth = h.participant(h.invitation())
    key = uid()
    first = h.upload(auth, request_id=key)
    assert first.status_code == 201
    second = h.upload(auth, request_id=key)
    assert second.status_code == 201 and second.json()["id"] == first.json()["id"]
    changed = h.upload(auth, request_id=key, content=b"changed-input")
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with h.repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Submission)) == 1
        assert shared_quota(db, h.episode)["count"] == 2


def test_pending_drafts_share_owner_upload_quota_and_adoption_is_not_double_counted(h):
    _, _, submission = h.submitted()
    with h.repo.factory() as db:
        before = shared_quota(db, h.episode)
    h.adopt(submission)
    with h.repo.factory() as db:
        assert shared_quota(db, h.episode) == before
    # Seven pending originals plus the owner's original exhaust the eight slots.
    auth = h.participant(h.invitation())
    for _ in range(6):
        assert h.upload(auth).status_code == 201
    assert h.upload(auth).status_code == 409
    with h.repo.factory() as db:
        assert shared_quota(db, h.episode)["count"] == 8


def test_participants_cannot_read_or_decide_each_others_drafts(h):
    _, _, submission = h.submitted()
    revision = h.client.post(BASE + f"/collaboration/submissions/{submission['id']}/revisions", headers=h.owner,
                             json={"text": "更短的说法", "start": 0, "end": 1}).json()
    other = h.participant(h.invitation(), "另一位")
    assert h.client.get(BASE + f"/participant/submissions/{submission['id']}/media").status_code == 404
    response = h.client.post(BASE + f"/participant/revisions/{revision['id']}/decision", headers=other, json={"decision": "confirmed"})
    assert response.status_code == 404
    assert h.client.get(BASE + "/participant/submissions").json()["items"] == []


def test_rewritten_expression_is_immutable_and_requires_actual_holder_confirmation(h):
    invitation, auth, submission = h.submitted()
    revision = h.client.post(BASE + f"/collaboration/submissions/{submission['id']}/revisions", headers=h.owner,
                             json={"text": "我在查菜谱", "start": .5, "end": 2}).json()
    route = BASE + f"/collaboration/revisions/{revision['id']}/adopt"
    assert h.client.post(route, headers=h.owner, json={}).status_code == 409
    decision = BASE + f"/participant/revisions/{revision['id']}/decision"
    assert h.client.post(decision, headers=h.owner, json={"decision": "confirmed"}).status_code == 403
    h.client.post(BASE + f"/collaboration/invitations/{invitation['id']}/close", headers=h.owner)
    assert h.client.post(decision, headers=auth, json={"decision": "confirmed"}).status_code == 200
    assert h.client.post(decision, headers=auth, json={"decision": "rejected"}).status_code == 409
    response = h.client.post(route, headers=h.owner, json={})
    assert response.status_code == 200
    assert h.client.post(route, headers=h.owner, json={}).json()["id"] == response.json()["id"]
    h.run()
    with h.repo.factory() as db:
        adoption = db.scalars(select(Adoption).where(Adoption.revision_id == revision["id"])).one()
        asset = db.get(Asset, adoption.asset_id)
        assert asset.duration == 1.5
        guard_asset(db, asset, {"start": 0, "end": 1.5, "caption": ""})
        with pytest.raises(DomainError, match="本人确认"):
            guard_asset(db, asset, {"start": 0, "end": 1, "caption": ""})
        with pytest.raises(DomainError):
            guard_asset(db, asset, {"start": 0, "end": 1.5, "caption": "额外改写"})


def test_new_submission_and_adoption_do_not_modify_old_versions_or_evidence(h):
    with h.repo.transaction() as db:
        episode = db.get(Episode, h.episode)
        episode.evidence_revision, episode.evidence_confirmed_revision = 7, 7
        version = Version(owner_id="alice", episode_id=h.episode, title="旧版", state="confirmed", confirmed_at="previous",
                          segments=[{"asset_id": h.source, "start": 0, "end": 2, "caption": ""}])
        db.add(version)
        db.flush()
        version_id = version.id
    _, _, submission = h.submitted()
    h.adopt(submission)
    with h.repo.factory() as db:
        assert db.get(Episode, h.episode).evidence_revision == 7
        version = db.get(Version, version_id)
        assert version.state == "confirmed" and version.confirmed_at == "previous"
        assert version.segments == [{"asset_id": h.source, "start": 0, "end": 2, "caption": ""}]


def test_scoped_objection_blocks_only_overlap_and_only_reporter_can_resolve(h):
    invitation = h.invitation("review")
    auth = h.participant(invitation)
    with h.repo.transaction() as db:
        affected = Version(owner_id="alice", episode_id=h.episode, title="有争议", state="confirmed", confirmed_at="before",
                           output_path="outputs/" + uid() + ".mp4", segments=[{"asset_id": h.source, "start": 0, "end": 5, "caption": ""}])
        safe = Version(owner_id="alice", episode_id=h.episode, title="无关", state="confirmed", confirmed_at="before",
                       segments=[{"asset_id": h.source, "start": 20, "end": 25, "caption": ""}])
        db.add_all([affected, safe])
        db.flush()
        ids = affected.id, safe.id
    response = h.client.post(BASE + "/participant/objections", headers=auth, json={"start": 1, "end": 3, "reason": "这个表达不准确"})
    assert response.status_code == 201
    objection = response.json()
    with h.repo.factory() as db:
        assert db.get(Version, ids[0]).state == "restricted"
        assert db.get(Version, ids[1]).state == "confirmed"
        with pytest.raises(DomainError):
            guard_asset(db, db.get(Asset, h.source), {"start": 2, "end": 4})
        guard_asset(db, db.get(Asset, h.source), {"start": 10, "end": 20})
    assert h.client.post(BASE + "/participant/objections", headers=auth, json={"start": 9, "end": 11, "reason": "越界"}).status_code == 403
    route = BASE + f"/participant/objections/{objection['id']}/resolve"
    assert h.client.post(route, headers=h.owner, json={"confirmed": True}).status_code == 403
    assert h.client.post(route, headers=auth, json={"confirmed": True}).status_code == 200
    with h.repo.factory() as db:
        restored = db.get(Version, ids[0])
        assert restored.state == "ready" and restored.confirmed_at is None


def test_withdrawal_revokes_old_asset_without_physically_deleting_media(h):
    _, auth, submission = h.submitted()
    asset_id = h.adopt(submission)
    with h.repo.factory() as db:
        source = h.repo.path(db.get(Submission, submission["id"]).source_path)
        derived = h.repo.path(db.get(Asset, asset_id).path)
    response = h.client.post(BASE + f"/participant/submissions/{submission['id']}/withdraw", headers=auth, json={"confirmed": True})
    assert response.status_code == 200
    cleanup_collaboration(h.repo)
    assert source.exists() and derived.exists()
    assert h.client.get(BASE + f"/assets/{asset_id}/media").status_code == 409
    assert h.client.get(BASE + f"/collaboration/submissions/{submission['id']}/media").status_code == 409
    episode = h.client.get(BASE + f"/episodes/{h.episode}").json()
    assert next(asset for asset in episode["assets"] if asset["id"] == asset_id)["interview_text"] is None


def test_review_role_cannot_upload_or_submit_unrequested_media(h):
    auth = h.participant(h.invitation("review"))
    assert h.upload(auth).status_code == 403
    response = h.client.post(BASE + "/participant/submissions", headers=auth, json={"client_request_id": uid(), "text": "新投稿"})
    assert response.status_code == 403


def test_text_submission_is_real_text_and_never_fabricates_audio(h):
    auth = h.participant(h.invitation())
    response = h.client.post(BASE + "/participant/submissions", headers=auth, json={"client_request_id": uid(), "text": "本人说明"})
    submission_id = response.json()["id"]
    response = h.client.post(BASE + f"/participant/submissions/{submission_id}/submit", headers=auth, json={"confirmed": True})
    assert response.status_code == 200
    revision_id = response.json()["revisions"][0]["id"]
    response = h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={})
    assert response.status_code == 409 and response.json()["error"]["code"] == "MEDIA_REQUIRED"


def test_confirmed_contribution_revision_is_materialized_and_cannot_be_rewritten_again(h, monkeypatch):
    _, auth, submission = h.submitted("contribute")
    original_asset = h.adopt(submission)
    with h.repo.factory() as db:
        # Original supply is ordinary footage and remains editable in the cut.
        guard_asset(db, db.get(Asset, original_asset), {"start": 0, "end": 1, "caption": "场景花字"})
    response = h.client.post(BASE + f"/collaboration/submissions/{submission['id']}/revisions", headers=h.owner,
                             json={"text": "本人补充的另一种说法", "start": 1, "end": 2})
    revision_id = response.json()["id"]
    assert h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={}).status_code == 409
    h.client.post(BASE + f"/participant/revisions/{revision_id}/decision", headers=auth, json={"decision": "confirmed"})
    from app.services import media
    def materialize(segments, output, title=""):
        assert segments[0]["start"] == 1 and segments[0]["end"] == 2
        assert segments[0]["caption"] == "本人确认补充：本人补充的另一种说法"
        output.write_bytes(b"offline-confirmed-contribution")
        return {"duration": 1, "width": 1280, "height": 720, "has_audio": True}
    monkeypatch.setattr(media, "render", materialize)
    response = h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={})
    h.run()
    with h.repo.factory() as db:
        job = db.get(Job, response.json()["id"])
        assert job.state == "succeeded", job.error
        asset = db.get(Asset, job.result["asset_id"])
        guard_asset(db, asset, {"start": 0, "end": 1, "caption": ""})
        with pytest.raises(DomainError) as error:
            guard_asset(db, asset, {"start": 0, "end": .5, "caption": ""})
        assert error.value.code == "EXPRESSION_FIXED"


def test_preview_retry_reuses_original_after_failure(h):
    auth = h.participant(h.invitation())
    submission = h.upload(auth).json()
    with h.repo.transaction() as db:
        job = db.get(Job, submission["preview_job_id"])
        job.state, job.error = "failed", {"code": "OFFLINE_FAILURE", "message": "测试失败"}
        source = db.get(Submission, submission["id"]).source_path
    response = h.client.post(BASE + f"/participant/submissions/{submission['id']}/retry", headers=auth, json={})
    assert response.status_code == 200 and response.json()["state"] == "queued"
    h.run()
    with h.repo.factory() as db:
        row = db.get(Submission, submission["id"])
        assert row.source_path == source and row.preview_path


def test_recheck_withdrawal_during_adoption_prevents_publication(h, monkeypatch):
    _, _, submission = h.submitted()
    revision_id = submission["revisions"][0]["id"]
    response = h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={})
    from app.services import interview_media
    original = interview_media.materialize_interview
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        with h.repo.transaction() as db:
            db.get(Submission, submission["id"]).state = "withdrawn"
        return result
    monkeypatch.setattr(interview_media, "materialize_interview", revoke)
    h.run()
    with h.repo.factory() as db:
        assert db.get(Job, response.json()["id"]).state == "failed"
        adoption = db.scalars(select(Adoption).where(Adoption.revision_id == revision_id)).one()
        assert adoption.asset_id is None
        assert adoption.output_path is None or not h.repo.path(adoption.output_path).exists()


def test_explicit_asset_delete_cleans_collaboration_original_and_derived_only(h):
    _, _, submission = h.submitted()
    asset_id = h.adopt(submission)
    with h.repo.factory() as db:
        row = db.get(Submission, submission["id"])
        files = [h.repo.path(row.source_path), h.repo.path(row.preview_path), h.repo.path(db.get(Asset, asset_id).path)]
        original = h.repo.path(db.get(Asset, h.source).path)
    response = h.client.delete(BASE + f"/assets/{asset_id}?confirm=true", headers=h.owner)
    assert response.status_code == 200, response.text
    assert all(not file.exists() for file in files)
    assert original.exists()


def test_migration_and_same_holder_session_survive_reopening_repository(h):
    invitation, auth, submission = h.submitted()
    from app.repository import Repository
    reopened = Repository(h.repo.data_dir)
    try:
        with reopened.factory() as db:
            assert db.get(Invitation, invitation["id"]).code_hash
            assert db.get(Submission, submission["id"]).state == "submitted"
            assert db.scalar(select(func.count()).select_from(ParticipantSession)) == 1
            assert db.scalar(select(func.count()).select_from(ExpressionRevision)) == 1
    finally:
        reopened.engine.dispose()
