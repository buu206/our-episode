"""HTTP/SQLite regressions for the exact private draft acknowledged by a holder.

Two independent participant sessions share one invitation identity. No media or
model calls are needed to reproduce the cross-tab edit/confirmation race.
"""
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from test_collaboration import BASE, h
from app.collaboration_models import ExpressionRevision, Submission
from app.models import uid


def private_text(h):
    invitation = h.invitation(questions=["第一个问题", "第二个问题"])
    auth = h.participant(invitation)
    response = h.client.post(BASE + "/participant/submissions", headers=auth,
        json={"text": "我核对的是这句原话", "question_id": invitation["questions"][0]["id"],
              "client_request_id": uid()})
    assert response.status_code == 201
    return invitation, auth, response.json()


@pytest.mark.parametrize("changed_field", ["text", "question_id"])
def test_another_session_cannot_change_the_draft_being_confirmed(h, changed_field):
    invitation, first_auth, draft = private_text(h)
    # A real second cookie and CSRF session, not a DB mutation or test substitute.
    second = TestClient(h.client.app, raise_server_exceptions=True)
    try:
        login = second.post(BASE + "/participant/auth/login", json={
            "invite_code": invitation["invite_code"], "display_name": "同一位本人"})
        assert login.status_code == 200
        assert login.json()["participant_id"] == draft["participant_id"]
        second_auth = {"X-CSRF-Token": login.json()["csrf_token"]}
        assert first_auth != second_auth
        assert second.get(BASE + "/auth/me").status_code == 401

        first_read = h.client.get(BASE + "/participant/submissions").json()["items"][0]
        expected = {key: first_read[key] for key in ("text", "question_id")}
        changed = {**expected, changed_field: "另一标签页改过的文字" if changed_field == "text"
                   else invitation["questions"][1]["id"]}
        edit = second.put(BASE + f"/participant/submissions/{draft['id']}",
                          headers=second_auth, json=changed)
        assert edit.status_code == 200

        route = BASE + f"/participant/submissions/{draft['id']}/submit"
        rejected = h.client.post(route, headers=first_auth,
                                 json={"confirmed": True, "expected_draft": expected})
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "DRAFT_CHANGED"
        with h.repo.factory() as db:
            stored = db.get(Submission, draft["id"])
            assert stored.state == "draft"
            assert stored.text == changed["text"] and stored.question_id == changed["question_id"]
            assert db.scalar(select(func.count()).select_from(ExpressionRevision)
                             .where(ExpressionRevision.submission_id == draft["id"])) == 0
        assert h.client.get(BASE + f"/episodes/{h.episode}/collaboration/submissions").json()["items"] == []

        accepted = h.client.post(route, headers=first_auth,
                                 json={"confirmed": True, "expected_draft": changed})
        assert accepted.status_code == 200
        assert accepted.json()["state"] == "submitted"
        assert accepted.json()["revisions"][0]["text"] == changed["text"]
        repeated = h.client.post(route, headers=first_auth,
                                 json={"confirmed": True, "expected_draft": changed})
        assert repeated.status_code == 200
        # Idempotent submission must not turn an older acknowledgement into success.
        assert h.client.post(route, headers=first_auth,
                             json={"confirmed": True, "expected_draft": expected}).status_code == 409
        with h.repo.factory() as db:
            assert db.scalar(select(func.count()).select_from(ExpressionRevision)
                             .where(ExpressionRevision.submission_id == draft["id"])) == 1
    finally:
        second.close()


def test_legacy_confirmation_remains_compatible_but_other_confirmation_endpoints_stay_strict(h):
    _, auth, draft = private_text(h)
    route = BASE + f"/participant/submissions/{draft['id']}"
    expected = {"text": draft["text"], "question_id": draft["question_id"]}
    rejected = h.client.post(route + "/withdraw", headers=auth,
                             json={"confirmed": True, "expected_draft": expected})
    assert rejected.status_code == 422
    accepted = h.client.post(route + "/submit", headers=auth, json={"confirmed": True})
    assert accepted.status_code == 200 and accepted.json()["state"] == "submitted"


def test_expected_draft_preserves_the_existing_literal_text_validation(h):
    _, auth, draft = private_text(h)
    route = BASE + f"/participant/submissions/{draft['id']}/submit"
    response = h.client.post(route, headers=auth,
        json={"confirmed": True, "expected_draft": {"text": "含有\n换行", "question_id": draft["question_id"]}})
    assert response.status_code == 422
    with h.repo.factory() as db:
        assert db.get(Submission, draft["id"]).state == "draft"
