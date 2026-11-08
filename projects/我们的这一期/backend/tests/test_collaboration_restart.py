"""Collaboration sessions must survive a new app instance without relogin.

This checks API persistence and role isolation. The independent process kill
and restart is exercised by verify.sh through scripts/restart-check.sh.
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


BASE = "/api/v1"


def test_invitation_and_separate_sessions_survive_application_restart(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", invites={"owner-test": "owner"},
                        worker_enabled=False)
    app = create_app(settings)
    with TestClient(app) as owner:
        login = owner.post(BASE + "/auth/login", json={"invite_code": "owner-test"})
        assert login.status_code == 200
        owner_headers = {"X-CSRF-Token": login.json()["csrf_token"]}
        response = owner.post(BASE + "/episodes", headers=owner_headers,
                              json={"title": "参与者恢复测试", "background": "离线工程验证"})
        assert response.status_code in (200, 201)
        episode_id = response.json()["id"]
        response = owner.post(BASE + f"/episodes/{episode_id}/collaboration/invitations",
                              headers=owner_headers, json={"role": "contribute",
                              "label": "恢复测试任务", "context": None, "questions": []})
        assert response.status_code in (200, 201)
        invitation = response.json()
        participant = TestClient(app)
        try:
            response = participant.post(BASE + "/participant/auth/login", json={
                "invite_code": invitation["invite_code"], "display_name": "参与者甲"})
            assert response.status_code == 200
            participant_headers = {"X-CSRF-Token": response.json()["csrf_token"]}
            identity = {key: response.json()[key]
                        for key in ("participant_id", "invite_id", "display_name")}
            assert identity["invite_id"] == invitation["id"]
            assert owner.cookies and participant.cookies
            assert set(owner.cookies).isdisjoint(set(participant.cookies))
            owner_cookies = dict(owner.cookies)
            participant_cookies = dict(participant.cookies)
            assert participant.get(BASE + "/episodes").status_code == 401
            assert owner.get(BASE + "/participant/auth/me").status_code == 401
        finally:
            participant.close()

    restarted_app = create_app(settings)
    with TestClient(restarted_app) as owner:
        owner.cookies.update(owner_cookies)
        response = owner.get(BASE + f"/episodes/{episode_id}/collaboration/invitations")
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["id"] for item in items] == [invitation["id"]]
        assert items[0]["label"] == "恢复测试任务"
        assert not items[0].get("invite_code")

        # Existing owner CSRF/session remain valid; no auth/login after restart.
        response = owner.post(BASE + f"/episodes/{episode_id}/collaboration/invitations",
                              headers=owner_headers, json={"role": "contribute",
                              "label": "重启后追加任务", "context": None, "questions": []})
        assert response.status_code in (200, 201)

        participant = TestClient(restarted_app)
        try:
            participant.cookies.update(participant_cookies)
            response = participant.get(BASE + "/participant/auth/me")
            assert response.status_code == 200
            assert {key: response.json()[key] for key in identity} == identity
            response = participant.get(BASE + "/participant/invitation")
            assert response.status_code == 200
            assert response.json()["id"] == invitation["id"]
            assert response.json()["label"] == "恢复测试任务"
            assert response.json()["closed"] is False
            assert participant.get(BASE + "/episodes").status_code == 401
            assert owner.get(BASE + "/participant/auth/me").status_code == 401
            response = participant.post(BASE + "/participant/auth/logout", headers=participant_headers)
            assert response.status_code == 200
            assert participant.get(BASE + "/participant/auth/me").status_code == 401
            assert owner.get(BASE + "/auth/me").status_code == 200
        finally:
            participant.close()
