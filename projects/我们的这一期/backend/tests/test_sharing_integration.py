"""Independent sharing boundaries over real local rendering and HTTP APIs.

No model or network service is used. Only the snapshot-drift and expiry cases
alter isolated DB records, explicitly simulating stale/corrupt persisted state;
confirmation, publication, objections, resolution and deletion use real APIs.
"""

from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path
import shutil
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.models import Asset, Job, Version, now, uid
from app.services.media import probe
from app.worker import Worker
from test_api import video_bytes  # Real FFmpeg source fixture; never user media.


BASE = "/api/v1"


def checked(response, status=200):
    # Never print authentication responses/cookies in test failure output.
    assert response.status_code == status
    return response.json()


def denied(response, status):
    allowed = status if isinstance(status, tuple) else (status,)
    assert response.status_code in allowed
    assert response.status_code not in (200, 206, 304)
    assert response.headers.get("cache-control") == "no-store"
    if response.request.method == "HEAD":
        assert response.content == b""
    else:
        assert response.headers.get("content-type", "").startswith("application/json")
        error = response.json()
        assert set(error) == {"error"}
        assert set(error["error"]) == {"code", "message"}
        assert "Traceback" not in error["error"]["message"]


@dataclass
class SharingHarness:
    app: object
    owner: TestClient
    owner_headers: dict = field(repr=False)
    episode_id: str
    asset_id: str
    clients: list = field(default_factory=list, repr=False)

    @property
    def repo(self):
        return self.app.state.repo

    def client(self):
        connection = TestClient(self.app, raise_server_exceptions=False)
        self.clients.append(connection)
        return connection

    def complete_job(self, job_id):
        for _ in range(12):
            Worker(self.repo, pipeline=self.app.state.pipeline).process_one()
            with self.repo.factory() as db:
                job = db.get(Job, job_id)
                state = job.state
                result = job.result
            if state in ("succeeded", "failed"):
                break
        assert state == "succeeded"
        return result

    def render(self, *, title="已确认分享版本", start=0, end=0.4, asset_id=None):
        version = checked(self.owner.post(BASE + f"/episodes/{self.episode_id}/versions",
            headers=self.owner_headers, json={"title": title, "packaging": "original",
            "segments": [{"asset_id": asset_id or self.asset_id, "start": start, "end": end,
                          "caption": ""}]}), 201)
        task = checked(self.owner.post(BASE + f"/versions/{version['id']}/render",
                                       headers=self.owner_headers, json={}))
        self.complete_job(task["id"])
        checked(self.owner.post(BASE + f"/versions/{version['id']}/confirm",
                                headers=self.owner_headers, json={}))
        with self.repo.factory() as db:
            saved = db.get(Version, version["id"])
            output = self.repo.path(saved.output_path)
            assert saved.state == "confirmed" and saved.confirmed_at
        info = probe(output)
        assert info["duration"] > 0
        assert min(info["width"], info["height"]) == 720
        return version["id"], output

    def share(self, version_id, label="仅此观看邀请"):
        return checked(self.owner.post(BASE + f"/versions/{version_id}/shares",
            headers=self.owner_headers, json={"label": label, "expires_days": 7,
            "confirmed": True, "client_request_id": uid()}), 201)

    def viewer(self, share):
        viewer = self.client()
        auth = checked(viewer.post(BASE + "/viewer/auth/login", json={
            "share_id": share["id"], "share_code": share["share_code"]}))
        assert auth["share_id"] == share["id"]
        return viewer, {"X-CSRF-Token": auth["csrf_token"]}

    def watch(self, viewer, share_id):
        return checked(viewer.get(BASE + f"/viewer/shares/{share_id}"))

    def current_share(self, share_id):
        items = checked(self.owner.get(BASE + f"/episodes/{self.episode_id}/shares"))["items"]
        return next(item for item in items if item["id"] == share_id)

    def update(self, share_id, version_id):
        current = self.current_share(share_id)
        return checked(self.owner.post(BASE + f"/shares/{share_id}/update",
            headers=self.owner_headers, json={"version_id": version_id,
            "expected_revision": current["revision"], "confirmed": True,
            "client_request_id": uid()}))

    def reviewer(self):
        invitation = checked(self.owner.post(BASE + f"/episodes/{self.episode_id}/collaboration/invitations",
            headers=self.owner_headers, json={"role": "review", "label": "内容核对者",
            "questions": [], "context": {"asset_id": self.asset_id, "start": 0, "end": 0.4}}), 201)
        participant = self.client()
        auth = checked(participant.post(BASE + "/participant/auth/login", json={
            "invite_code": invitation["invite_code"], "display_name": "内容核对者"}))
        return participant, {"X-CSRF-Token": auth["csrf_token"]}


@pytest.fixture
def sharing(tmp_path, video_bytes, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("sharing integration tests must not access a network service")

    async def no_async_network(*args, **kwargs):
        raise AssertionError("sharing integration tests must not access a network service")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_network)
    app = create_app(Settings(data_dir=tmp_path / "local-sharing", invites={
        "share-owner-test": "alice", "share-other-test": "bob"}, worker_enabled=False))
    with TestClient(app, raise_server_exceptions=False) as owner:
        auth = checked(owner.post(BASE + "/auth/login", json={"invite_code": "share-owner-test"}))
        headers = {"X-CSRF-Token": auth["csrf_token"]}
        episode = checked(owner.post(BASE + "/episodes", headers=headers,
                                     json={"title": "受控分享接口验证"}), 201)
        asset = checked(owner.post(BASE + f"/episodes/{episode['id']}/assets", headers=headers,
            files={"file": ("local-source.mp4", video_bytes, "video/mp4")}), 201)
        harness = SharingHarness(app, owner, headers, episode["id"], asset["id"])
        try:
            yield harness
        finally:
            for client in harness.clients:
                client.close()


def assert_revoked_media(viewer, url, status, cached):
    """Cached validators, HEAD and Range must all go through live authorization."""
    etag = cached.headers.get("etag", "*")
    modified = cached.headers.get("last-modified", formatdate(time.time() + 3600, usegmt=True))
    variants = [
        ("GET", {}),
        ("HEAD", {}),
        ("GET", {"Range": "bytes=0-15"}),
        ("HEAD", {"Range": "bytes=0-15"}),
        ("GET", {"If-None-Match": etag}),
        ("HEAD", {"If-None-Match": etag}),
        ("GET", {"If-Modified-Since": modified}),
        ("GET", {"Range": "bytes=0-15", "If-Range": etag}),
    ]
    for method, headers in variants:
        denied(viewer.request(method, url, headers=headers), status)


def test_real_output_viewer_get_head_and_range_are_authorized(sharing):
    version_id, output = sharing.render()
    grant = sharing.share(version_id)
    viewer, _ = sharing.viewer(grant)
    watch = sharing.watch(viewer, grant["id"])
    assert watch["version_id"] == version_id and watch["revision"] == grant["revision"]
    assert "revision=" in watch["media_url"]
    assert not {"segments", "evidence", "background", "owner_id", "output_path"}.intersection(watch)
    response = viewer.get(watch["media_url"])
    assert response.status_code == 200 and response.content == output.read_bytes()
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("video/mp4")
    assert viewer.head(watch["media_url"]).status_code == 200
    partial = viewer.get(watch["media_url"], headers={"Range": "bytes=0-15"})
    assert partial.status_code == 206 and partial.content == response.content[:16]
    assert partial.headers["content-range"].startswith("bytes 0-15/")
    malformed_range = viewer.get(watch["media_url"], headers={"Range": "bytes=0-1,4-5"})
    denied(malformed_range, 416)
    assert malformed_range.headers["content-range"] == f"bytes */{len(response.content)}"


def test_explicit_update_preserves_expiry_and_revokes_old_media_url(sharing):
    first_id, first_output = sharing.render()
    grant = sharing.share(first_id)
    viewer, _ = sharing.viewer(grant)
    initial = sharing.watch(viewer, grant["id"])
    cached = viewer.get(initial["media_url"])
    second_id, second_output = sharing.render(title="另存的新版本", start=0.1, end=0.4)
    # Merely making and confirming a candidate does not replace the publication.
    assert sharing.watch(viewer, grant["id"])["version_id"] == first_id
    assert viewer.get(initial["media_url"]).content == first_output.read_bytes()
    updated = sharing.update(grant["id"], second_id)
    assert updated["revision"] == grant["revision"] + 1
    assert updated["expires_at"] == grant["expires_at"]
    assert_revoked_media(viewer, initial["media_url"], 409, cached)
    current = sharing.watch(viewer, grant["id"])
    assert current["version_id"] == second_id
    assert current["media_url"] != initial["media_url"]
    assert viewer.get(current["media_url"]).content == second_output.read_bytes()


def test_repeated_confirmation_and_completed_render_preserve_publication(sharing):
    version_id, _ = sharing.render()
    grant = sharing.share(version_id)
    viewer, _ = sharing.viewer(grant)
    media_url = sharing.watch(viewer, grant["id"])["media_url"]
    with sharing.repo.factory() as db:
        version = db.get(Version, version_id)
        original = (version.confirmed_at, version.output_path, version.job_id)
    checked(sharing.owner.post(BASE + f"/versions/{version_id}/confirm", headers=sharing.owner_headers, json={}))
    render = checked(sharing.owner.post(BASE + f"/versions/{version_id}/render", headers=sharing.owner_headers, json={}))
    assert render["id"] == original[2] and render["state"] == "succeeded"
    with sharing.repo.factory() as db:
        version = db.get(Version, version_id)
        assert (version.confirmed_at, version.output_path, version.job_id) == original
    assert sharing.current_share(grant["id"])["state"] == "active"
    assert viewer.get(media_url).status_code == 200


@pytest.mark.parametrize("changed_field", ["output_path", "confirmed_at", "state"])
def test_persisted_snapshot_drift_never_serves_a_different_or_unconfirmed_file(sharing, changed_field):
    version_id, output = sharing.render()
    grant = sharing.share(version_id)
    viewer, _ = sharing.viewer(grant)
    old_url = sharing.watch(viewer, grant["id"])["media_url"]
    cached = viewer.get(old_url)
    # Explicit fault injection into the test database, not a product edit API.
    with sharing.repo.transaction() as db:
        version = db.get(Version, version_id)
        if changed_field == "output_path":
            replacement = "outputs/" + uid() + ".mp4"
            shutil.copyfile(output, sharing.repo.path(replacement))
            version.output_path = replacement
        elif changed_field == "confirmed_at":
            version.confirmed_at = now()
        else:
            version.state = "ready"
    assert_revoked_media(viewer, old_url, 409, cached)
    denied(viewer.get(BASE + "/viewer/auth/me"), 409)


def test_owner_participant_and_viewer_cookies_do_not_grant_each_others_roles(sharing):
    version_id, _ = sharing.render()
    grant = sharing.share(version_id)
    viewer, viewer_headers = sharing.viewer(grant)
    participant, _ = sharing.reviewer()
    assert set(viewer.cookies).isdisjoint(set(sharing.owner.cookies))
    assert set(viewer.cookies).isdisjoint(set(participant.cookies))
    protected = ["/auth/me", "/episodes", f"/episodes/{sharing.episode_id}",
                 f"/assets/{sharing.asset_id}/media", f"/versions/{version_id}/media",
                 f"/versions/{version_id}/export", "/participant/auth/me", "/participant/invitation"]
    for path in protected:
        denied(viewer.get(BASE + path), 401)
    denied(viewer.post(BASE + "/episodes", headers=viewer_headers, json={"title": "无写权限"}), 401)
    denied(participant.get(BASE + f"/viewer/shares/{grant['id']}"), 401)
    denied(sharing.owner.get(BASE + f"/viewer/shares/{grant['id']}"), 401)


def test_invitation_scope_and_other_owner_cannot_be_swapped_by_identifier(sharing):
    version_id, _ = sharing.render()
    first = sharing.share(version_id, "第一位观众")
    second = sharing.share(version_id, "第二位观众")
    viewer_a, _ = sharing.viewer(first)
    viewer_b, _ = sharing.viewer(second)
    second_url = sharing.watch(viewer_b, second["id"])["media_url"]
    assert viewer_b.get(second_url).status_code == 200
    denied(viewer_a.get(BASE + f"/viewer/shares/{second['id']}"), (403, 404))
    denied(viewer_a.get(second_url, headers={"Range": "bytes=0-15"}), (403, 404))
    unknown = sharing.client()
    denied(unknown.post(BASE + "/viewer/auth/login", json={
        "share_id": second["id"], "share_code": first["share_code"]}), 401)
    other_owner = sharing.client()
    auth = checked(other_owner.post(BASE + "/auth/login", json={"invite_code": "share-other-test"}))
    other_headers = {"X-CSRF-Token": auth["csrf_token"]}
    denied(other_owner.get(BASE + f"/episodes/{sharing.episode_id}/shares"), 404)
    denied(other_owner.post(BASE + f"/shares/{first['id']}/close", headers=other_headers,
        json={"expected_revision": first["revision"], "confirmed": True}), 404)
    assert sharing.watch(viewer_a, first["id"])["id"] == first["id"]


def test_objection_suspends_immediately_and_resolution_never_reopens_automatically(sharing):
    from app.sharing_models import Share

    affected_id, _ = sharing.render(start=0, end=0.2)
    safe_id, _ = sharing.render(title="无争议区间", start=0.3, end=0.5)
    affected, safe = sharing.share(affected_id), sharing.share(safe_id, "安全版本观众")
    viewer, _ = sharing.viewer(affected)
    safe_viewer, _ = sharing.viewer(safe)
    old_url = sharing.watch(viewer, affected["id"])["media_url"]
    cached = viewer.get(old_url)
    safe_url = sharing.watch(safe_viewer, safe["id"])["media_url"]
    participant, auth = sharing.reviewer()
    objection = checked(participant.post(BASE + "/participant/objections", headers=auth,
        json={"start": 0.05, "end": 0.1, "reason": "这段需要先核对"}), 201)
    # The lifecycle hook must suspend the grant before any viewer request can
    # accidentally make a lazy status check look like proactive invalidation.
    with sharing.repo.factory() as db:
        assert db.get(Share, affected["id"]).state == "restricted"
        assert db.get(Share, safe["id"]).state == "active"
    assert_revoked_media(viewer, old_url, 409, cached)
    assert safe_viewer.get(safe_url).status_code == 200
    checked(participant.post(BASE + f"/participant/objections/{objection['id']}/resolve",
                              headers=auth, json={"confirmed": True}))
    checked(sharing.owner.post(BASE + f"/versions/{affected_id}/confirm",
                                headers=sharing.owner_headers, json={}))
    denied(viewer.get(old_url), 409)
    assert sharing.current_share(affected["id"])["state"] == "restricted"
    updated = sharing.update(affected["id"], affected_id)
    assert updated["state"] == "active" and updated["revision"] > affected["revision"]
    new_url = sharing.watch(viewer, affected["id"])["media_url"]
    assert viewer.get(new_url).status_code == 200
    denied(viewer.get(old_url), 409)


def test_failed_physical_delete_still_revokes_viewing_and_retry_does_not_restore_it(sharing, monkeypatch):
    from app.sharing_models import Share

    version_id, _ = sharing.render()
    grant = sharing.share(version_id)
    viewer, _ = sharing.viewer(grant)
    url = sharing.watch(viewer, grant["id"])["media_url"]
    cached = viewer.get(url)
    impact = checked(sharing.owner.get(BASE + f"/assets/{sharing.asset_id}/impact"))
    affected = {item["id"]: item for item in impact["shares"]}
    assert affected[grant["id"]]["version_id"] == version_id
    assert affected[grant["id"]]["label"] == grant["label"]
    assert affected[grant["id"]]["state"] == "active"
    with sharing.repo.factory() as db:
        source_path = sharing.repo.path(db.get(Asset, sharing.asset_id).path)
    unlink = Path.unlink

    def temporarily_locked(path, *args, **kwargs):
        if path == source_path:
            raise PermissionError("isolated test deletion failure")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", temporarily_locked)
        denied(sharing.owner.delete(BASE + f"/assets/{sharing.asset_id}?confirm=true",
                                     headers=sharing.owner_headers), 503)
        assert source_path.exists()
        with sharing.repo.factory() as db:
            assert db.get(Share, grant["id"]).state == "restricted"
        assert_revoked_media(viewer, url, 409, cached)
    checked(sharing.owner.delete(BASE + f"/assets/{sharing.asset_id}?confirm=true",
                                  headers=sharing.owner_headers))
    assert not source_path.exists()
    denied(viewer.get(url), 409)


def test_delete_impact_includes_other_confirmed_adoptions_of_the_same_submission(sharing):
    from app.sharing_models import Share

    invitation = checked(sharing.owner.post(BASE + f"/episodes/{sharing.episode_id}/collaboration/invitations",
        headers=sharing.owner_headers, json={"role": "contribute", "label": "供材参与者",
        "questions": [], "context": None}), 201)
    participant = sharing.client()
    auth = checked(participant.post(BASE + "/participant/auth/login", json={
        "invite_code": invitation["invite_code"], "display_name": "供材参与者"}))
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    with sharing.repo.factory() as db:
        source = sharing.repo.path(db.get(Asset, sharing.asset_id).path).read_bytes()
    draft = checked(participant.post(BASE + "/participant/submissions/upload", headers=headers,
        data={"kind": "video", "client_request_id": uid(), "text": "本人原始供材"},
        files={"file": ("participant-source.mp4", source, "video/mp4")}), 201)
    sharing.complete_job(draft["preview_job_id"])
    submitted = checked(participant.post(BASE + f"/participant/submissions/{draft['id']}/submit",
                                         headers=headers, json={"confirmed": True}))
    first_revision = submitted["revisions"][0]["id"]
    revision = checked(sharing.owner.post(BASE + f"/collaboration/submissions/{draft['id']}/revisions",
        headers=sharing.owner_headers, json={"start": 0.1, "end": 0.4, "text": "本人确认的新表达"}), 201)
    checked(participant.post(BASE + f"/participant/revisions/{revision['id']}/decision",
                              headers=headers, json={"decision": "confirmed"}))
    adopted_assets = []
    publications = []
    outputs = []
    for revision_id in (first_revision, revision["id"]):
        task = checked(sharing.owner.post(BASE + f"/collaboration/revisions/{revision_id}/adopt",
                                          headers=sharing.owner_headers, json={}))
        asset_id = sharing.complete_job(task["id"])["asset_id"]
        adopted_assets.append(asset_id)
        with sharing.repo.factory() as db:
            duration = db.get(Asset, asset_id).duration
        version_id, output = sharing.render(asset_id=asset_id, end=duration)
        publications.append(sharing.share(version_id))
        outputs.append(output)
    impact = checked(sharing.owner.get(BASE + f"/assets/{adopted_assets[0]}/impact"))
    assert {item["id"] for item in impact["shares"]} == {item["id"] for item in publications}
    viewers = [sharing.viewer(item)[0] for item in publications]
    urls = [sharing.watch(viewer, item["id"])["media_url"] for viewer, item in zip(viewers, publications)]
    checked(sharing.owner.delete(BASE + f"/assets/{adopted_assets[0]}?confirm=true",
                                  headers=sharing.owner_headers))
    with sharing.repo.factory() as db:
        assert all(db.get(Asset, asset_id).deleted for asset_id in adopted_assets)
        assert all(db.get(Share, item["id"]).state == "restricted" for item in publications)
    assert all(not output.exists() for output in outputs)
    for viewer, url in zip(viewers, urls):
        denied(viewer.get(url), 409)


def test_closed_grant_denies_cached_media_and_allows_only_session_logout(sharing):
    version_id, _ = sharing.render()
    grant = sharing.share(version_id)
    viewer, auth = sharing.viewer(grant)
    url = sharing.watch(viewer, grant["id"])["media_url"]
    cached = viewer.get(url)
    closed = checked(sharing.owner.post(BASE + f"/shares/{grant['id']}/close",
        headers=sharing.owner_headers, json={"expected_revision": grant["revision"], "confirmed": True}))
    assert closed["state"] == "closed"
    assert_revoked_media(viewer, url, 410, cached)
    denied(viewer.get(BASE + "/viewer/auth/me"), 410)
    denied(sharing.owner.post(BASE + f"/shares/{grant['id']}/update", headers=sharing.owner_headers,
        json={"version_id": version_id, "expected_revision": closed["revision"],
              "confirmed": True, "client_request_id": uid()}), 410)
    checked(viewer.post(BASE + "/viewer/auth/logout", headers=auth, json={}))
    denied(viewer.get(url), 401)


def test_expired_grant_denies_old_session_login_head_and_conditional_media(sharing):
    from app.sharing_models import Share

    version_id, _ = sharing.render()
    grant = sharing.share(version_id)
    viewer, _ = sharing.viewer(grant)
    url = sharing.watch(viewer, grant["id"])["media_url"]
    cached = viewer.get(url)
    with sharing.repo.transaction() as db:
        db.get(Share, grant["id"]).expires_at = time.time() - 1
    assert_revoked_media(viewer, url, 410, cached)
    denied(viewer.get(BASE + "/viewer/auth/me"), 410)
    newcomer = sharing.client()
    denied(newcomer.post(BASE + "/viewer/auth/login", json={
        "share_id": grant["id"], "share_code": grant["share_code"]}), 410)
