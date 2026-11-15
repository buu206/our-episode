#!/usr/bin/env bash
# verify.sh owns the kill/restart/comparison. This hook only performs each action.
set -euo pipefail
PRODUCT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export RESTART_BACKEND="$PRODUCT_DIR/backend"
exec "$RESTART_BACKEND/.venv/bin/python" - "$@" <<'PY'
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import httpx


BACKEND = Path(os.environ["RESTART_BACKEND"]).resolve()
TEST_INVITE = "restart-test"


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)


raw_dir = os.environ.get("RESTART_CHECK_DIR")
if not raw_dir:
    fail("缺少 verify.sh 提供的 RESTART_CHECK_DIR；拒绝使用正常数据目录。")
CHECK_DIR = Path(raw_dir).resolve()
if (not CHECK_DIR.is_dir() or CHECK_DIR == Path("/")
        or CHECK_DIR == BACKEND.parent or CHECK_DIR.is_relative_to(BACKEND)
        or CHECK_DIR.is_relative_to(BACKEND.parent / "data")):
    fail("RESTART_CHECK_DIR 不是有效的独立临时目录。")
STATE = CHECK_DIR / "restart-instance.json"
COLLABORATION = CHECK_DIR / "restart-collaboration.json"
SHARING = CHECK_DIR / "restart-sharing.json"
EDIT_DRAFT = CHECK_DIR / "restart-edit-draft.json"
EDITORIAL_DRAFT = CHECK_DIR / "restart-editorial-draft.json"
SUBTITLE_DRAFT = CHECK_DIR / "restart-subtitle-draft.json"
RETENTION = CHECK_DIR / "restart-retention.json"
DIRECTOR = CHECK_DIR / "restart-director.json"


def read_state():
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if (type(state["pid"]) is not int or state["pid"] <= 1
                or type(state["port"]) is not int or not 1 <= state["port"] <= 65535):
            raise ValueError("invalid state")
        return state
    except (OSError, ValueError, KeyError, TypeError):
        fail("重启检查实例状态缺失或无效。")


def is_our_process(state):
    result = subprocess.run(["ps", "-ww", "-p", str(state["pid"]), "-o", "stat=", "-o", "args="],
                            text=True, capture_output=True, check=False)
    line = result.stdout.strip()
    return (result.returncode == 0 and line and not line.startswith("Z")
            and "uvicorn" in line and "app.main:production_app" in line and str(BACKEND) in line
            and f'--port {state["port"]} ' in line)


def stop(state):
    if not is_our_process(state):
        return
    try:
        os.kill(state["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and is_our_process(state):
            time.sleep(0.1)
        if is_our_process(state):
            os.kill(state["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def client_for(state):
    base = f'http://127.0.0.1:{state["port"]}'
    return httpx.Client(base_url=base, timeout=3, trust_env=False, follow_redirects=False,
                        headers={"Origin": base})


def login(client):
    response = client.post("/api/v1/auth/login", json={"invite_code": TEST_INVITE})
    if response.status_code != 200:
        fail("重启检查登录失败。")
    try:
        csrf = response.json()["csrf_token"]
        if not isinstance(csrf, str) or not csrf:
            raise ValueError("missing csrf")
    except (ValueError, TypeError, KeyError):
        fail("登录响应缺少 CSRF 令牌。")
    client.headers["X-CSRF-Token"] = csrf


def save_private_record(destination, record):
    # The invitation secret is never retained. Session credentials stay only in
    # verify's private temporary directory and are never printed or logged.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CHECK_DIR,
                                         prefix=".restart-credentials-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle)
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_collaboration(owner, state, episode_id):
    response = owner.post(f"/api/v1/episodes/{episode_id}/collaboration/invitations", json={
        "role": "contribute", "label": "重启验收参与者", "context": None, "questions": [],
    })
    if response.status_code not in (200, 201):
        fail("通过产品接口创建重启检查邀请失败。")
    invitation = response.json()
    if not all(isinstance(invitation.get(key), str) and invitation[key] for key in ("id", "invite_code")):
        fail("邀请响应缺少有效标识。")
    with client_for(state) as participant:
        response = participant.post("/api/v1/participant/auth/login", json={
            "invite_code": invitation["invite_code"], "display_name": "重启验收参与者",
        })
        if response.status_code != 200:
            fail("重启检查参与者登录失败。")
        identity = response.json()
        if (identity.get("invite_id") != invitation["id"]
                or identity.get("display_name") != "重启验收参与者"
                or not all(isinstance(identity.get(key), str) and identity[key]
                           for key in ("participant_id", "csrf_token"))):
            fail("参与者登录响应缺少有效身份或会话信息。")
        cookies = [{"name": cookie.name, "value": cookie.value,
                    "domain": cookie.domain, "path": cookie.path}
                   for cookie in participant.cookies.jar]
        if not cookies:
            fail("参与者登录未建立独立会话。")
        save_private_record(COLLABORATION, {
            "episode_id": episode_id, "invite_id": invitation["id"],
            "participant_id": identity["participant_id"],
            "display_name": identity["display_name"],
            "csrf_token": identity["csrf_token"], "cookies": cookies,
        })


def read_collaboration(owner, state, episode_id):
    record = json.loads(COLLABORATION.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("episode_id") != episode_id
            or not all(isinstance(record.get(key), str) and record[key]
                       for key in ("invite_id", "participant_id", "display_name", "csrf_token"))
            or not isinstance(record.get("cookies"), list) or not record["cookies"]):
        fail("参与者重启检查记录缺失或无效。")
    response = owner.get(f"/api/v1/episodes/{episode_id}/collaboration/invitations")
    if response.status_code != 200:
        fail("重启后发起人无法读取邀请。")
    invitations = response.json()["items"]
    matches = [item for item in invitations if item["id"] == record["invite_id"]]
    if (len(matches) != 1 or matches[0].get("role") != "contribute"
            or matches[0].get("label") != "重启验收参与者" or matches[0].get("closed") is not False
            or matches[0].get("invite_code")):
        fail("重启后邀请内容不一致或重新暴露了一次性凭据。")
    with client_for(state) as participant:
        # No participant login here: the exact session issued before the process
        # restart must still work against the new process and its persisted DB.
        for cookie in record["cookies"]:
            if (not isinstance(cookie, dict)
                    or not all(isinstance(cookie.get(key), str) and cookie[key]
                               for key in ("name", "value", "domain", "path"))):
                fail("参与者重启检查会话无效。")
            participant.cookies.set(cookie["name"], cookie["value"],
                                    domain=cookie["domain"], path=cookie["path"])
        participant.headers["X-CSRF-Token"] = record["csrf_token"]
        response = participant.get("/api/v1/participant/auth/me")
        if response.status_code != 200:
            fail("进程重启后原参与者会话不可用。")
        identity = response.json()
        if any(identity.get(key) != record[key] for key in ("participant_id", "invite_id", "display_name")):
            fail("重启后参与者身份发生变化。")
        response = participant.get("/api/v1/participant/invitation")
        if response.status_code != 200:
            fail("重启后参与者无法读取被邀任务。")
        invitation = response.json()
        if (invitation.get("id") != record["invite_id"] or invitation.get("role") != "contribute"
                or invitation.get("closed") is not False):
            fail("重启后参与者任务归属或状态发生变化。")
        if participant.get("/api/v1/episodes").status_code != 401:
            fail("参与者会话错误获得了发起人访问权限。")


def confirmed_test_version(owner, episode_id):
    """Use the normal HTTP render/confirm flow, never seed a fake ready file."""
    binary = shutil.which("ffmpeg")
    if not binary and Path("/opt/homebrew/bin/ffmpeg").is_file():
        binary = "/opt/homebrew/bin/ffmpeg"
    if not binary:
        fail("分享重启检查需要本机 FFmpeg。")
    source = CHECK_DIR / "restart-share-source.mp4"
    result = subprocess.run(
        [binary, "-nostdin", "-v", "error", "-n", "-threads", "2",
         "-filter_threads", "1", "-f", "lavfi", "-i",
         "color=c=blue:size=160x90:rate=10", "-t", "3", "-an",
         "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", str(source)],
        check=False, capture_output=True, timeout=20, shell=False,
    )
    if result.returncode != 0:
        fail("分享重启检查的本机测试视频生成失败。")
    with source.open("rb") as handle:
        response = owner.post(f"/api/v1/episodes/{episode_id}/assets",
                              files={"file": ("restart-share-source.mp4", handle, "video/mp4")},
                              timeout=15)
    if response.status_code not in (200, 201):
        fail("分享重启检查上传测试视频失败。")
    asset_id = response.json()["id"]
    # Persist the same fields compiled from a model edit plan, through the normal
    # API, without enabling cloud processing or manufacturing a model response.
    draft = {"title": "重启检查编导草案", "packaging": "variety_v2",
             "narration_text": "待人工确认的测试片头。", "segments": [
                 {"asset_id": asset_id, "start": .4, "end": .8, "caption": "后半段",
                  "visual_beats": [{"kind": "cutout", "source_time": .4, "duration": .4,
                                    "target": {"x": .2, "y": .2, "width": .4, "height": .4},
                                    "contour": [{"x": .3, "y": .2}, {"x": .5, "y": .4}, {"x": .3, "y": .6}],
                                    "text": "细节", "position": "bottom_right"}]},
                 {"asset_id": asset_id, "start": 0, "end": .3, "caption": "前半段"}]}
    response = owner.post(f"/api/v1/episodes/{episode_id}/versions", json=draft)
    if response.status_code != 201:
        fail("重启检查创建编导草案失败。")
    # Compare the complete API contract before and after restart. Old manual
    # requests omit optional director metadata; the response returns defaults.
    expected_segments = [{"purpose": "", "evidence_ids": [], "transition": "cut",
                          "transition_duration": 0, "visual_beats": [], **item} for item in draft["segments"]]
    saved = response.json()
    if saved.get("segments") != expected_segments:
        fail("保存草案时片段内容或默认剪辑参数变化。")
    save_private_record(EDIT_DRAFT, {**draft, "segments": expected_segments, "id": saved["id"]})
    evidence_id = 'restart-editorial-evidence'
    # A labelled synthetic fixture in verify's isolated database, never a model
    # result or real user evidence. The public API cannot invent analysis IDs.
    # The version itself is created/read through HTTP, then survives real restart.
    fixture = [{'id': evidence_id, 'kind': 'visual', 'asset_id': asset_id,
                'start': 0., 'end': 3., 'note': '重启检查蓝色合成图形，非真实模型。'},
               {'id':'restart-synthetic-speech','kind':'speech','asset_id':asset_id,'start':.1,'end':.5,'note':'合成字幕夹具'}]
    with sqlite3.connect(CHECK_DIR / 'data' / 'app.sqlite3') as db:
        changed = db.execute(
            'update episodes set evidence=?, evidence_confirmed_revision=evidence_revision, '
            'analysis_state=?, analysis_asset_ids=? where id=?',
            (json.dumps(fixture, ensure_ascii=False), 'confirmed', json.dumps([asset_id]), episode_id))
        if changed.rowcount != 1:
            fail('隔离合成来源夹具未保存。')
    editorial = owner.post(f'/api/v1/episodes/{episode_id}/versions', json={
        'title': '重启检查后期编导安排', 'packaging': 'variety_v2', 'segments': [
            {'asset_id': asset_id, 'start': 0., 'end': 3., 'caption': '',
             'purpose': '合成素材版式测试', 'evidence_ids': [evidence_id],
             'editorial': {'role': 'setup', 'treatment': 'chapter', 'reason': '验证版式参数恢复',
                           'evidence_ids': [evidence_id], 'source_time': .2, 'duration': 2.,
                           'text': '合成测试', 'detail': '这是一块蓝色画面'}}]})
    if editorial.status_code != 201:
        fail('重启检查的后期编导安排保存失败。')
    saved_editorial = editorial.json()
    save_private_record(EDITORIAL_DRAFT, {'id': saved_editorial['id'], 'segments': saved_editorial['segments']})
    track = owner.get(f"/api/v1/versions/{saved_editorial['id']}/subtitles")
    if track.status_code != 200 or not track.json()['rows']:
        fail('隔离字幕轨道读取失败。')
    segments = saved_editorial['segments']
    segments[0]['subtitle_edits'] = [{'cue_id':track.json()['rows'][0]['cue_id'], 'text':'校对后的合成字幕', 'hidden':True, 'reviewed':True}]
    corrected = owner.post(f'/api/v1/episodes/{episode_id}/versions', json={
        'title':'重启字幕校对夹具', 'base_version_id':saved_editorial['id'], 'packaging':'variety_v2', 'segments':segments})
    if corrected.status_code != 201:
        fail('隔离字幕校对保存失败。')
    save_private_record(SUBTITLE_DRAFT, {'id':corrected.json()['id']})
    response = owner.post(f"/api/v1/episodes/{episode_id}/versions", json={
        "title": "重启验收固定版本", "packaging": "original", "narration_text": "",
        "segments": [{"asset_id": asset_id, "start": 0, "end": 0.6, "caption": ""}],
    })
    if response.status_code not in (200, 201):
        fail("分享重启检查创建版本失败。")
    version_id = response.json()["id"]
    response = owner.post(f"/api/v1/versions/{version_id}/render", json={})
    if response.status_code not in (200, 201, 202):
        fail("分享重启检查提交真实渲染失败。")
    job_id = response.json()["id"]
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = owner.get(f"/api/v1/jobs/{job_id}")
        if response.status_code != 200:
            fail("分享重启检查无法读取渲染任务。")
        job_state = response.json()["state"]
        if job_state == "succeeded":
            break
        if job_state == "failed":
            fail("分享重启检查真实渲染失败。")
        time.sleep(0.1)
    else:
        fail("分享重启检查等待真实渲染超时。")
    response = owner.post(f"/api/v1/versions/{version_id}/confirm", json={})
    if (response.status_code != 200 or response.json().get("id") != version_id
            or response.json().get("state") != "confirmed"):
        fail("分享重启检查无法确认已渲染版本。")
    return version_id


def check_viewer_scope(viewer, version_id):
    for route in ("/api/v1/episodes", "/api/v1/participant/auth/me",
                  f"/api/v1/versions/{version_id}/export"):
        if viewer.get(route).status_code != 401:
            fail("观看会话错误获得了发起人或参与者权限。")


def check_share_media(viewer, media_url, *, allowed):
    response = viewer.get(media_url, headers={"Range": "bytes=0-127"})
    if allowed:
        if (response.status_code not in (200, 206)
                or not response.headers.get("content-type", "").startswith("video/mp4")
                or not response.content):
            fail("有效分享的原观看会话无法读取固定版本媒体。")
    else:
        if response.status_code != 410:
            fail("已关闭分享仍允许媒体获取或返回了非受控错误。")
        error = response.json().get("error", {})
        if not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
            fail("已关闭分享未返回统一错误结构。")
    head = viewer.head(media_url)
    if allowed and head.status_code != 200:
        fail("有效分享的媒体 HEAD 请求失败。")
    if not allowed and head.status_code != 410:
        fail("已关闭分享仍允许媒体 HEAD 请求。")


def write_sharing(owner, state, episode_id):
    version_id = confirmed_test_version(owner, episode_id)
    records = []
    for should_close in (False, True):
        label = "重启验收已关闭观看者" if should_close else "重启验收观看者"
        response = owner.post(f"/api/v1/versions/{version_id}/shares", json={
            "label": label, "expires_days": 7, "confirmed": True,
            "client_request_id": str(uuid.uuid4()),
        })
        if response.status_code not in (200, 201):
            fail("通过产品接口创建重启检查分享失败。")
        share = response.json()
        if (share.get("episode_id") != episode_id or share.get("version_id") != version_id
                or share.get("label") != label or share.get("state") != "active"
                or not isinstance(share.get("id"), str) or not share["id"]
                or not isinstance(share.get("share_code"), str) or not share["share_code"]
                or type(share.get("revision")) is not int):
            fail("分享创建响应缺少有效归属、状态或凭据。")
        with client_for(state) as viewer:
            response = viewer.post("/api/v1/viewer/auth/login", json={
                "share_id": share["id"], "share_code": share["share_code"],
            })
            if response.status_code != 200:
                fail("重启检查观看者登录失败。")
            identity = response.json()
            if (identity.get("share_id") != share["id"] or identity.get("label") != label
                    or not isinstance(identity.get("csrf_token"), str) or not identity["csrf_token"]):
                fail("观看者登录未返回独立身份与 CSRF。")
            cookies = [{"name": cookie.name, "value": cookie.value,
                        "domain": cookie.domain, "path": cookie.path}
                       for cookie in viewer.cookies.jar]
            if not cookies or set(viewer.cookies) & set(owner.cookies):
                fail("观看者登录未建立独立 Cookie 会话。")
            response = viewer.get(f'/api/v1/viewer/shares/{share["id"]}')
            if response.status_code != 200:
                fail("观看者无法读取刚创建的分享。")
            media_url = response.json().get("media_url")
            expected_media = f'/api/v1/viewer/shares/{share["id"]}/media?revision={share["revision"]}'
            if media_url != expected_media:
                fail("分享媒体地址不符合受限同源接口契约。")
            check_share_media(viewer, media_url, allowed=True)
            original_expiry, original_revision = share["expires_at"], share["revision"]
            if should_close:
                response = owner.post(f'/api/v1/shares/{share["id"]}/close', json={
                    "expected_revision": original_revision, "confirmed": True,
                })
                if response.status_code != 200 or response.json().get("state") != "closed":
                    fail("分享重启检查关闭操作失败。")
                share = response.json()
                check_share_media(viewer, media_url, allowed=False)
            else:
                response = owner.post(f'/api/v1/shares/{share["id"]}/update', json={
                    "version_id": version_id, "expected_revision": original_revision, "confirmed": True,
                    "client_request_id": str(uuid.uuid4()),
                })
                if (response.status_code != 200 or response.json().get("revision") != original_revision + 1
                        or response.json().get("expires_at") != original_expiry):
                    fail("分享同版明确更新未递增修订或错误延长了期限。")
                share = response.json()
                media_url = f'/api/v1/viewer/shares/{share["id"]}/media?revision={share["revision"]}'
                check_share_media(viewer, media_url, allowed=True)
            check_viewer_scope(viewer, version_id)
            records.append({key: share[key] for key in (
                "id", "episode_id", "version_id", "label", "state", "revision", "expires_at", "closed_at",
            )} | {"cookies": cookies, "csrf_token": identity["csrf_token"], "media_url": media_url})
    save_private_record(SHARING, {"episode_id": episode_id, "version_id": version_id, "shares": records})


def read_sharing(owner, state, episode_id):
    subtitles = json.loads(SUBTITLE_DRAFT.read_text(encoding='utf-8'))
    checked = owner.get(f"/api/v1/versions/{subtitles['id']}/subtitles")
    if checked.status_code != 200 or not checked.json()['rows']:
        fail('重启后校对字幕无法读取。')
    row = checked.json()['rows'][0]
    if row['text'] != '校对后的合成字幕' or not row['hidden'] or not row['reviewed']:
        fail('重启后字幕正文、隐藏或校对状态变化。')
    draft = json.loads(EDIT_DRAFT.read_text(encoding="utf-8"))
    response = owner.get(f"/api/v1/episodes/{episode_id}")
    if response.status_code != 200:
        fail("重启后无法读取编导草案。")
    editorial = json.loads(EDITORIAL_DRAFT.read_text(encoding='utf-8'))
    items = [v for v in response.json()['versions'] if v['id'] == editorial['id']]
    if len(items) != 1 or items[0]['segments'] != editorial['segments'] or items[0]['state'] != 'draft':
        fail('重启后后期编导节点、版式或待确认状态变化。')
    versions = [item for item in response.json()["versions"] if item["id"] == draft["id"]]
    if (len(versions) != 1 or any(versions[0].get(key) != value for key, value in draft.items())
            or versions[0].get("narration_approved") is not False
            or versions[0].get("narration_ready") is not False):
        fail("重启后片段顺序、花字、旁白草案或人工确认状态变化。")
    record = json.loads(SHARING.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("episode_id") != episode_id
            or not isinstance(record.get("version_id"), str)
            or not isinstance(record.get("shares"), list) or len(record["shares"]) != 2):
        fail("分享重启检查记录缺失或无效。")
    response = owner.get(f"/api/v1/episodes/{episode_id}/shares")
    if response.status_code != 200:
        fail("重启后发起人无法读取分享列表。")
    items = response.json()["items"]
    for expected in record["shares"]:
        matches = [item for item in items if item["id"] == expected["id"]]
        if (len(matches) != 1 or any(matches[0].get(key) != expected[key] for key in (
                "id", "episode_id", "version_id", "label", "state", "revision", "expires_at", "closed_at"))
                or matches[0].get("share_code") or matches[0].get("watch_url")):
            fail("重启后分享固定版本、修订、期限或关闭状态变化，或再次暴露了凭据。")
        with client_for(state) as viewer:
            # Never log in again: restore the exact persisted pre-restart session.
            if not isinstance(expected.get("cookies"), list) or not expected["cookies"]:
                fail("观看者重启检查会话缺失。")
            for cookie in expected["cookies"]:
                if (not isinstance(cookie, dict)
                        or not all(isinstance(cookie.get(key), str) and cookie[key]
                                   for key in ("name", "value", "domain", "path"))):
                    fail("观看者重启检查会话无效。")
                viewer.cookies.set(cookie["name"], cookie["value"],
                                   domain=cookie["domain"], path=cookie["path"])
            viewer.headers["X-CSRF-Token"] = expected["csrf_token"]
            allowed = expected["state"] == "active"
            response = viewer.get("/api/v1/viewer/auth/me")
            if allowed:
                if (response.status_code != 200 or response.json().get("share_id") != expected["id"]
                        or response.json().get("label") != expected["label"]
                        or response.json().get("csrf_token") != expected["csrf_token"]):
                    fail("进程重启后原观看会话未保持身份与 CSRF。")
            elif response.status_code != 410:
                fail("已关闭观看会话恢复时返回了非受控错误。")
            response = viewer.get(f'/api/v1/viewer/shares/{expected["id"]}')
            if allowed:
                if (response.status_code != 200 or any(response.json().get(key) != expected[key]
                        for key in ("id", "version_id", "label", "revision", "expires_at"))
                        or response.json().get("media_url") != expected["media_url"]):
                    fail("重启后观看者未取得原分享的固定版本。")
            elif response.status_code != 410:
                fail("重启后已关闭分享重新允许观看。")
            check_share_media(viewer, expected["media_url"], allowed=allowed)
            check_viewer_scope(viewer, record["version_id"])


def write_retention(client, episode_id):
    response = client.get(f'/api/v1/episodes/{episode_id}/retention')
    if response.status_code != 200 or not response.json()['expires_at']:
        fail('留存重启检查缺少保存期限。')
    deadline = response.json()['expires_at']
    response = client.post('/api/v1/episodes', json={'title': '待删除的重启测试空节目'})
    if response.status_code != 201:
        fail('留存重启检查无法创建测试记录。')
    deleted_id = response.json()['id']
    if client.delete(f'/api/v1/episodes/{deleted_id}?confirm=true').status_code != 200:
        fail('留存重启检查无法删除测试记录。')
    save_private_record(RETENTION, {'episode_id': episode_id, 'expires_at': deadline, 'deleted_id': deleted_id})


def read_retention(client, episode_id):
    expected = json.loads(RETENTION.read_text(encoding='utf-8'))
    response = client.get(f'/api/v1/episodes/{episode_id}/retention')
    if (expected['episode_id'] != episode_id or response.status_code != 200
            or response.json()['expires_at'] != expected['expires_at']):
        fail('重启改变了节目保存期限。')
    if client.get(f'/api/v1/episodes/{expected["deleted_id"]}').status_code != 410:
        fail('重启后已删除节目重新可见。')
    response = client.get(f'/api/v1/episodes/{expected["deleted_id"]}/retention')
    if response.status_code != 200 or response.json()['cleanup_pending'] or not response.json()['deletion_requested']:
        fail('重启后删除完成状态丢失。')


def write_director(client, episode_id):
    response = client.post(f'/api/v1/episodes/{episode_id}/director/messages', json={
        'revision': 0, 'client_id': 'restart-director-focus', 'choice': 'conversation'})
    if response.status_code != 200:
        fail('导演需求重启检查无法保存选择。')
    first = response.json()
    if first.get('revision') != 1 or first.get('focus') != 'conversation':
        fail('导演需求保存结果与选择不一致。')
    response = client.post(f'/api/v1/episodes/{episode_id}/director/messages', json={
        'revision': 1, 'client_id': 'restart-director-note', 'text': '保留完整原声，花字少一点。'})
    if response.status_code != 200:
        fail('导演需求重启检查无法保存补充。')
    state = response.json()
    if state.get('revision') != 2 or state.get('notes') != '保留完整原声，花字少一点。':
        fail('导演补充需求保存不一致。')
    save_private_record(DIRECTOR, state)


def read_director(client, episode_id):
    expected = json.loads(DIRECTOR.read_text(encoding='utf-8'))
    response = client.get(f'/api/v1/episodes/{episode_id}')
    if response.status_code != 200 or response.json().get('director') != expected:
        fail('重启后导演需求、对话记录或修订状态变化。')
    retry = client.post(f'/api/v1/episodes/{episode_id}/director/messages', json={
        'revision': 1, 'client_id': 'restart-director-note', 'text': '保留完整原声，花字少一点。'})
    if retry.status_code != 200 or retry.json() != expected:
        fail('重启后重复发送同一条导演需求没有保持幂等。')


def start():
    if STATE.exists() and is_our_process(read_state()):
        fail("临时检查实例仍在运行；拒绝重复启动或替换现有进程。")
    env = os.environ.copy()
    env.update({"APP_DATA_DIR": str(CHECK_DIR / "data"), "APP_INVITE_CODE": TEST_INVITE,
                "CLOUD_ENABLED": "false", "CLOUD_RETENTION_VERIFIED": "false",
                "CLOUD_REGION_VERIFIED": "false", "MODEL_API_KEY": "",
                "MODEL_BASE_URL": "", "MODEL_NAME": "", "WEB_CONCURRENCY": "1",
                "PYTHON_DOTENV_DISABLED": "1"})
    # Pass a prebound localhost socket: no port selection race and no other process is killed.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        command = [str(BACKEND / ".venv/bin/python"), "-m", "uvicorn", "--factory", "app.main:production_app",
                   "--app-dir", str(BACKEND), "--host", "127.0.0.1", "--port", str(port),
                   "--fd", str(listener.fileno()), "--workers", "1", "--no-access-log"]
        with (CHECK_DIR / "server.log").open("ab") as log:
            process = subprocess.Popen(command, cwd=BACKEND, env=env, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True,
                                       pass_fds=(listener.fileno(),))
    state = {"pid": process.pid, "port": port}
    STATE.write_text(json.dumps(state), encoding="utf-8")
    STATE.chmod(0o600)
    deadline = time.monotonic() + 25
    with client_for(state) as client:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                response = client.get("/")
                if response.status_code == 200:
                    print(process.pid)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    stop(state)
    fail("临时服务未能就绪；诊断日志位于 RESTART_CHECK_DIR/server.log。")


try:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "start":
        start()
    elif action in ("write", "read"):
        if len(sys.argv) != 3 or not sys.argv[2]:
            fail("write/read 需要一个非空参数。")
        state = read_state()
        if not is_our_process(state):
            fail("重启检查实例未运行。")
        with client_for(state) as client:
            login(client)
            if action == "write":
                response = client.post("/api/v1/episodes", json={"title": sys.argv[2],
                                                               "background": "restart persistence check"})
                if response.status_code not in (200, 201):
                    fail("通过产品接口写入恢复检查记录失败。")
                record_id = response.json().get("id")
                if not isinstance(record_id, str) or not record_id:
                    fail("写入接口未返回记录 ID。")
                write_collaboration(client, state, record_id)
                write_sharing(client, state, record_id)
                write_retention(client, record_id)
                write_director(client, record_id)
                print(record_id)
            else:
                response = client.get("/api/v1/episodes")
                if response.status_code != 200:
                    fail("通过产品接口读取恢复检查记录失败。")
                items = response.json()["items"]
                matches = [item for item in items if item["id"] == sys.argv[2]]
                if len(matches) != 1 or not isinstance(matches[0]["title"], str):
                    fail("重启后未找到唯一的持久记录。")
                read_collaboration(client, state, sys.argv[2])
                read_sharing(client, state, sys.argv[2])
                read_retention(client, sys.argv[2])
                read_director(client, sys.argv[2])
                print(matches[0]["title"])
    elif action == "stop":
        if STATE.exists():
            stop(read_state())
    else:
        fail("用法：restart-check.sh start | write 标记 | read ID | stop")
except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
    # Keep responses, cookies, CSRF values and local credentials out of verify output.
    fail("重启检查执行失败；请核对临时实例及接口契约。")
PY
