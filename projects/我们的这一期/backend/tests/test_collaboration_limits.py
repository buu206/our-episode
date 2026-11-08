"""Role-specific upload caps and original-source quota regressions.

Media inspection is explicitly replaced by controlled metadata. Large byte caps
are tested by scaling only the interview cap for streaming checks and by staging
quota counters; no large or personal media is generated or uploaded externally.
"""
from sqlalchemy import func, select
import pytest

from test_collaboration import BASE, h
from app.collaboration_models import Adoption, Submission
from app.config import Settings
from app.models import Asset, Job, uid
from app.services import collaboration, interview_media, media


def source_duration(monkeypatch, seconds):
    info = {"duration": seconds, "width": 1280, "height": 720, "has_audio": True, "size": 12}
    monkeypatch.setattr(media, "probe", lambda *_: info.copy())
    monkeypatch.setattr(interview_media, "probe_interview", lambda *_: info.copy())
    def preview(source, output):
        output.write_bytes(b"explicit-offline-preview")
        return info.copy()
    monkeypatch.setattr(interview_media, "preview_contribution", preview)


def test_production_role_caps_match_authorized_limits():
    settings = Settings()
    assert collaboration.submission_limits("interview", settings) == (100_000_000, 60)
    assert collaboration.submission_limits("contribute", settings) == (1024 ** 3, 600)


@pytest.mark.parametrize("role,seconds,status", [
    ("contribute", 120, 201),
    ("interview", 60, 201),
    ("interview", 60.001, 422),
])
def test_only_interviews_have_a_sixty_second_per_file_cap(h, monkeypatch, role, seconds, status):
    source_duration(monkeypatch, seconds)
    auth = h.participant(h.invitation(role))
    response = h.upload(auth, kind="video")
    assert response.status_code == status, response.text
    if status == 201:
        assert response.json()["duration"] == seconds
    else:
        assert response.json()["error"]["code"] == "DURATION_EXCEEDED"
        with h.repo.factory() as db:
            assert collaboration.shared_quota(db, h.episode)["count"] == 1


def test_contribution_can_use_full_ten_minutes_when_no_other_originals_occupy_quota(h, monkeypatch):
    source_duration(monkeypatch, 600)
    with h.repo.transaction() as db:
        db.get(Asset, h.source).deleted = True
    auth = h.participant(h.invitation("contribute"))
    response = h.upload(auth, kind="video")
    assert response.status_code == 201, response.text
    assert response.json()["duration"] == 600
    with h.repo.factory() as db:
        assert collaboration.shared_quota(db, h.episode)["seconds"] == 600


def test_contribution_stream_and_idempotent_retry_do_not_use_interview_byte_cap(h, monkeypatch):
    # Scale the interview-only threshold to exercise the same read-loop branch
    # with small byte fixtures. Production caps are asserted separately above.
    monkeypatch.setattr(collaboration, "MAX_INTERVIEW_BYTES", 10)
    auth = h.participant(h.invitation("contribute"))
    key, content = uid(), b"a" * 20
    first = h.upload(auth, kind="video", request_id=key, content=content)
    assert first.status_code == 201, first.text
    repeat = h.upload(auth, kind="video", request_id=key, content=content)
    assert repeat.status_code == 201 and repeat.json()["id"] == first.json()["id"]
    different = h.upload(auth, kind="video", request_id=key, content=b"b" * 20)
    assert different.status_code == 409 and different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    interview = h.participant(h.invitation("interview"))
    blocked = h.upload(interview, kind="video", content=content)
    assert blocked.status_code == 413 and blocked.json()["error"]["code"] == "UPLOAD_TOO_LARGE"


def test_full_byte_quota_allows_same_successful_request_but_rejects_new_bytes(h):
    content, key = b"small-input", uid()
    with h.repo.transaction() as db:
        db.get(Asset, h.source).size = 1024 ** 3 - len(content)
    auth = h.participant(h.invitation("contribute"))
    first = h.upload(auth, kind="video", request_id=key, content=content)
    assert first.status_code == 201, first.text
    repeated = h.upload(auth, kind="video", request_id=key, content=content)
    assert repeated.status_code == 201 and repeated.json()["id"] == first.json()["id"]
    exceeded = h.upload(auth, kind="video", content=b"one-more-byte")
    assert exceeded.status_code == 413 and exceeded.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    with h.repo.factory() as db:
        assert collaboration.shared_quota(db, h.episode)["bytes"] == 1024 ** 3
        assert db.scalar(select(func.count()).select_from(Submission).where(Submission.state == "draft")) == 1


def test_remaining_duration_is_checked_again_when_contribution_commits(h, monkeypatch):
    source_duration(monkeypatch, 540)
    auth = h.participant(h.invitation("contribute"))
    key = uid()
    first = h.upload(auth, kind="video", request_id=key)
    assert first.status_code == 201, first.text  # Existing owner source is 60 s.
    assert h.upload(auth, kind="video", request_id=key).status_code == 201
    source_duration(monkeypatch, 1)
    exceeded = h.upload(auth, kind="video")
    assert exceeded.status_code == 409 and exceeded.json()["error"]["code"] == "DURATION_EXCEEDED"
    with h.repo.factory() as db:
        quota = collaboration.shared_quota(db, h.episode)
        assert quota["seconds"] == 600 and quota["count"] == 2


def test_long_unedited_supply_can_be_adopted_but_long_fixed_expression_is_explicitly_rejected(h, monkeypatch):
    source_duration(monkeypatch, 120)
    _, auth, submitted = h.submitted("contribute")
    adopted_id = h.adopt(submitted)
    with h.repo.factory() as db:
        assert db.get(Asset, adopted_id).duration == 120
    proposed = h.client.post(BASE + f"/collaboration/submissions/{submitted['id']}/revisions", headers=h.owner,
                             json={"text": "需要本人重新确认的补充说明", "start": 0, "end": 100})
    assert proposed.status_code == 201
    revision_id = proposed.json()["id"]
    decided = h.client.post(BASE + f"/participant/revisions/{revision_id}/decision", headers=auth,
                            json={"decision": "confirmed"})
    assert decided.status_code == 200
    response = h.client.post(BASE + f"/collaboration/revisions/{revision_id}/adopt", headers=h.owner, json={})
    assert response.status_code == 409 and response.json()["error"]["code"] == "EXPRESSION_TOO_LONG"
    with h.repo.factory() as db:
        assert db.scalars(select(Adoption).where(Adoption.revision_id == revision_id)).first() is None
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "accept_submission")) == 1
        assert db.get(Asset, adopted_id).duration == 120  # Never silently truncated.
