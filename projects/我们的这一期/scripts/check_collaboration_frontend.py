#!/usr/bin/env python3
"""Real three-role Next/FastAPI/media acceptance; no model calls or user data."""
from contextlib import ExitStack
import hashlib
from uuid import uuid4
import httpx
import check_frontend_transport as transport
from check_frontend_transport import API, checked, denied, require, wait_for


def run_checks(base, source, _metrics, _process, directory, secrets_seen):
    with ExitStack() as stack:
        owner, participant, viewer, outsider = [stack.enter_context(httpx.Client(
            base_url=base, timeout=45, trust_env=False, headers={"Origin": base})) for _ in range(4)]

        def login(client, path, body):
            response = checked(client.post(API + path, json=body), 200, "role login")
            auth = response.json()
            client.headers["X-CSRF-Token"] = auth["csrf_token"]
            secrets_seen.append(auth["csrf_token"])
            secrets_seen.extend(cookie.value for cookie in client.cookies.jar)
            require("httponly" in response.headers.get("set-cookie", "").lower(), "HttpOnly role cookie")
            return auth

        def post(client, path, body=None, status=200):
            return checked(client.post(API + path, json={} if body is None else body), status, path).json()

        def get(client, path):
            return checked(client.get(API + path), 200, path).json()

        def job_finished(job):
            def poll():
                current = get(owner, "/jobs/" + job["id"])
                require(current["state"] != "failed", "local media job failed")
                return current["state"] == "succeeded"
            wait_for(poll, "local media job timeout", 50)

        def ranged(client, url):
            response = checked(client.get(url, headers={"Range": "bytes=0-127"}), 206, "authorized media Range")
            require(len(response.content) == 128 and response.headers.get("cache-control") == "no-store", "Range bytes/cache policy")

        login(owner, "/auth/login", {"invite_code": "transport-a"})
        login(outsider, "/auth/login", {"invite_code": "transport-b"})
        episode = post(owner, "/episodes", {"title": "3-2三角色技术验收"}, 201)
        eid = episode["id"]
        with source.open("rb") as stream:
            asset = checked(owner.post(API + f"/episodes/{eid}/assets", files={"file": (source.name, stream, "video/quicktime")}), 201, "owner source upload").json()
        invitation = post(owner, f"/episodes/{eid}/collaboration/invitations", {
            "role": "interview", "label": "技术测试参与者", "context": {"asset_id": asset["id"], "start": 0, "end": .7},
            "questions": ["这一秒有什么声音？"]}, 201)
        secrets_seen.append(invitation["invite_code"])
        denied(owner.get(API + "/participant/auth/me"), 401, "owner alone cannot act as participant")
        login(participant, "/participant/auth/login", {"invite_code": invitation["invite_code"], "display_name": "技术测试参与者"})
        denied(participant.get(API + f"/episodes/{eid}"), 401, "participant has no owner access")
        denied(outsider.get(API + f"/episodes/{eid}/collaboration/submissions"), 404, "other owner isolation")
        wait_for(lambda: get(participant, "/participant/invitation")["context_state"] == "ready", "context preparation", 30)
        ranged(participant, API + "/participant/context/media")
        denied(outsider.get(API + "/participant/context/media"), 401, "context is role limited")
        question = get(participant, "/participant/invitation")["questions"][0]
        question.update({"text": "我想换个问法", "skipped": True})
        checked(participant.put(API + "/participant/invitation/questions", json={"questions": [question]}), 200, "edit and skip own question")
        require(get(participant, "/participant/invitation")["questions"][0]["skipped"], "question skip persisted")
        draft = post(participant, "/participant/submissions", {"text": "私密文字稍后删除", "client_request_id": str(uuid4())}, 201)
        request_id = str(uuid4())
        with source.open("rb") as stream:
            submitted = checked(participant.post(API + "/participant/submissions/upload", data={
                "kind": "video", "text": "技术声音测试", "client_request_id": request_id},
                files={"file": (source.name, stream, "video/quicktime")}), 201, "participant upload above 10 MiB").json()
        sid = submitted["id"]
        require(not get(owner, f"/episodes/{eid}/collaboration/submissions")["items"], "private drafts leaked to owner")
        denied(owner.get(API + f"/collaboration/submissions/{sid}/media"), 404, "owner cannot view private media")

        def draft_ready():
            found = next(item for item in get(participant, "/participant/submissions")["items"] if item["id"] == sid)
            require(found["preview_state"] != "failed", "participant preview failed")
            return found["preview_state"] == "ready"
        wait_for(draft_ready, "participant media preview", 40)
        ranged(participant, API + f"/participant/submissions/{sid}/media")
        checked(participant.put(API + f"/participant/submissions/{sid}", json={"text": "更新后的技术表达", "question_id": None}), 200, "same draft changed in another request")
        denied(participant.post(API + f"/participant/submissions/{sid}/submit", json={"confirmed": True, "expected_draft": {"text": "技术声音测试", "question_id": None}}), 409, "stale consent cannot confirm new wording", "DRAFT_CHANGED")
        submitted = post(participant, f"/participant/submissions/{sid}/submit", {"confirmed": True, "expected_draft": {"text": "更新后的技术表达", "question_id": None}})
        ranged(owner, API + f"/collaboration/submissions/{sid}/media")
        revision = post(owner, f"/collaboration/submissions/{sid}/revisions", {"text": "本人同意后才采用", "start": .1, "end": .7}, 201)
        denied(owner.post(API + f"/collaboration/revisions/{revision['id']}/adopt", json={}), 409, "unconfirmed revision cannot be adopted")
        post(participant, f"/participant/revisions/{revision['id']}/decision", {"decision": "confirmed"})
        job_finished(post(owner, f"/collaboration/revisions/{revision['id']}/adopt"))
        updated = get(owner, f"/episodes/{eid}")
        adopted = next(item for item in updated["assets"] if item["source_kind"] == "interview")

        def render(source_asset, title):
            interval = source_asset.get("fixed_segment") or {"start": 0, "end": .6}
            version = post(owner, f"/episodes/{eid}/versions", {"title": title, "packaging": "original", "narration_text": "", "segments": [{"asset_id": source_asset["id"], **interval, "caption": ""}]}, 201)
            job_finished(post(owner, f"/versions/{version['id']}/render"))
            return post(owner, f"/versions/{version['id']}/confirm")

        version = render(adopted, "包含本人已确认表达")
        exported = checked(owner.get(version["media_url"]), 200, "real rendered version")
        require(len(exported.content) > 1000, "rendered output empty")
        share = post(owner, f"/versions/{version['id']}/shares", {"label": "技术观看者", "expires_days": 1, "confirmed": True, "client_request_id": str(uuid4())}, 201)
        secrets_seen.append(share["share_code"])
        login(viewer, "/viewer/auth/login", {"share_id": share["id"], "share_code": share["share_code"]})
        denied(viewer.get(API + f"/episodes/{eid}"), 401, "viewer cannot read creator data")
        denied(participant.get(API + f"/viewer/shares/{share['id']}"), 401, "participant cannot act as viewer")
        published = get(viewer, f"/viewer/shares/{share['id']}")
        old_url = published["media_url"]
        ranged(viewer, old_url)
        checked(viewer.head(old_url), 200, "viewer HEAD")
        viewed = checked(viewer.get(old_url), 200, "viewer full media")
        require(hashlib.sha256(viewed.content).digest() == hashlib.sha256(exported.content).digest(), "published video bytes mismatch")
        denied(participant.post(API + f"/participant/submissions/{sid}/withdraw", json={"confirmed": True},
            headers={"X-CSRF-Token": owner.headers["X-CSRF-Token"]}), 403, "cross-role CSRF rejected")
        objection = post(participant, "/participant/objections", {"submission_id": sid, "reason": "技术验证：暂时停止使用这段"}, 201)
        denied(viewer.get(old_url, headers={"Range": "bytes=0-127"}), 409, "objection revokes viewer Range")
        denied(viewer.get(API + "/viewer/auth/me"), 409, "restricted viewer state")
        post(participant, f"/participant/objections/{objection['id']}/resolve", {"confirmed": True})
        # Resolving an objection must not silently restore a published link.
        denied(viewer.get(old_url), 409, "resolved objection does not auto-republish")
        safe_version = render(asset, "重新确认的安全版本")
        renewed = post(owner, f"/shares/{share['id']}/update", {"version_id": safe_version["id"], "expected_revision": share["revision"], "confirmed": True, "client_request_id": str(uuid4())})
        require(renewed["expires_at"] == share["expires_at"] and renewed["revision"] == 2, "share update extended expiry or lost revision")
        denied(viewer.get(old_url), 409, "old media revision remains invalid")
        new_published = get(viewer, f"/viewer/shares/{share['id']}")
        ranged(viewer, new_published["media_url"])
        post(owner, f"/shares/{share['id']}/close", {"expected_revision": 2, "confirmed": True})
        denied(viewer.get(new_published["media_url"], headers={"Range": "bytes=0-127"}), 410, "closed share Range")
        post(viewer, "/viewer/auth/logout")
        denied(viewer.get(API + "/viewer/auth/me"), 401, "closed viewer can still log out")
        post(owner, f"/collaboration/invitations/{invitation['id']}/close")
        post(participant, f"/participant/submissions/{sid}/withdraw", {"confirmed": True})
        denied(owner.get(API + f"/collaboration/submissions/{sid}/media"), 409, "withdrawn source no longer accessible")
        checked(participant.delete(API + f"/participant/submissions/{draft['id']}?confirm=true"), 200, "delete only private draft after invitation closure")
        require(get(owner, "/auth/me")["user_id"] == "transport-owner-a", "other role logout affected owner")
        return {"participant_upload_bytes": source.stat().st_size, "roles_isolated_over_real_proxy": True,
            "private_draft_hidden_until_explicit_submission": True, "context_range_and_question_skip": True,
            "revision_requires_participant_confirmation": True, "draft_confirmation_atomic_snapshot": True, "real_media_preview_adopt_render": True,
            "viewer_full_video_sha256_match": True, "objection_blocks_without_auto_republish": True,
            "share_update_revision_and_fixed_expiry": True, "old_revision_and_closed_range_denied": True,
            "closed_invitation_keeps_withdrawal_and_private_delete": True, "restricted_viewer_logout": True}


if __name__ == "__main__":
    raise SystemExit(transport.main(run_checks, "阶段3-2 三角色正式代理与媒体验收"))
