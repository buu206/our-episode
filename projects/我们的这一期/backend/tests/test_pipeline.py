"""Offline workflow acceptance: fake model decisions, real media and persistence.

FakeServices is injected only by these tests and explicitly has is_real=False.
Every paid-operation substitute must obtain the real persisted permit first.
These tests demonstrate engineering flow, not real model quality or admission.
"""

from collections import Counter
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import socket
import struct
import subprocess
import wave

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import Asset, Budget, Job, ModelCall, Version
from app.services import media
from app.services.director import DirectorError
from app.services.pipeline import ModelServices
from app.worker import Worker


BASE = "/api/v1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Pipeline tests must never use a network transport")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", denied)


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "pipeline-data", invites={"test-only": "owner"},
                    worker_enabled=False, cloud={})


@pytest.fixture(scope="session")
def voiced_video_bytes(tmp_path_factory):
    path = tmp_path_factory.mktemp("pipeline-synthetic-media") / "source.mp4"
    subprocess.run([media._binary("ffmpeg"), "-nostdin", "-v", "error", "-threads", "2",
        "-filter_threads", "1", "-f", "lavfi", "-i", "color=c=blue:s=96x64:r=10:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-threads", "2",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(path)],
        check=True, capture_output=True, timeout=30, shell=False)
    assert media.probe(path)["has_audio"] is True
    return path.read_bytes()


class FakeServices(ModelServices):
    """Explicit fixtures, not an alternate production mode or a local AI model."""

    is_real = False
    costs = {"vision": .01, "asr": .00022, "directions": .02, "tts": .00016}

    def __init__(self, *, failures=None):
        super().__init__({})
        self.counts = Counter()
        self.preparations = Counter()
        self.failures = {kind: list(values) for kind, values in (failures or {}).items()}
        self.permissions = []
        self.forge_direction = False

    def ready(self):
        return True, "Offline injected test services; not a real AI result."

    def prepare_vision(self, assets, media_root):
        self.preparations["vision"] += 1
        return super().prepare_vision(assets, media_root)

    def prepare_audio(self, asset, path, directory):
        self.preparations["audio"] += 1
        return super().prepare_audio(asset, path, directory)

    def _permission(self, kind, reservation, permit, required=None, digest=None):
        # No outcome, fee, file or counter is produced until the real durable
        # ledger and source-bound permission have been checked.
        assert permit(reservation, required, digest) is True
        self.permissions.append((kind, reservation, required, digest))
        self.counts[kind] += 1
        failure = self.failures.get(kind, [])
        if failure:
            mode = failure.pop(0)
            if mode == "unknown":
                raise DirectorError("MODEL_TIMEOUT", "Test-only unknown response; keep reservation.",
                    usage={"cost_yuan": None, "cost_status": "unknown", "model": "fixture-only"})
            if mode == "known":
                raise DirectorError("TEST_KNOWN_FAILURE", "Test-only recoverable response.",
                    usage=self._usage(kind))

    def _usage(self, kind):
        return {"model": "fixture-only-" + kind, "input_tokens": 10,
                "output_tokens": 10, "retries": 0, "cost_yuan": self.costs[kind],
                "cost_status": "known", "request_id": "offline-" + kind}

    async def vision(self, prepared, reservation, permit):
        self._permission("vision", reservation, permit, prepared.required_reservation_yuan,
                         prepared.request_digest)
        return {"result": {"observations": [{"asset_id": f.asset_id, "time": f.time,
            "frame_id": f.frame_id, "note": "测试替身观察：画面为蓝色。", "verification": "unverified"}
            for f in prepared.frames], "verification": "unverified",
            "uncertainties": ["测试数据，不是真实模型判断；仅检查稀疏帧。"],
            "coverage": [c.model_dump() for c in prepared.coverage]}, "usage": self._usage("vision")}

    async def asr(self, chunks, reservation, permit):
        self._permission("asr", reservation, permit)
        # The actual fixture is a tone, not speech; this labelled text only
        # exercises review/version contracts, never transcription accuracy.
        return {"result": [{"asset_id": c.asset_id, "start": c.offset + .1,
            "end": c.offset + min(.6, c.duration), "text": "测试替身转写，请人工核对"}
            for c in chunks], "usage": self._usage("asr")}

    async def directions(self, evidence, assets, background, reservation, permit):
        self._permission("directions", reservation, permit)
        span = dict(evidence[0])
        if self.forge_direction:
            span["end"] = assets[span["asset_id"]] + 10
        return {"result": {"directions": [{"id": "test-direction", "title": "测试这一刻",
            "summary": "测试替身返回的有来源方向，不代表真实模型效果。", "evidence": [span]}],
            "uncertainties": ["测试方向仍需人工选择。"]}, "usage": self._usage("directions")}

    async def tts(self, text, output, reservation, permit):
        self._permission("tts", reservation, permit)
        assert text and isinstance(output, Path) and not output.exists()
        # Real PCM WAV, 0.6 s non-silent tone. No synthesizer/model is installed.
        rate = 24000
        pcm = b"".join(struct.pack("<h", round(6000 * math.sin(2 * math.pi * 660 * i / rate)))
                       for i in range(round(rate * .6)))
        with wave.open(str(output), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(pcm)
        return {"result": {"path": str(output), "duration": .6}, "usage": self._usage("tts")}


@pytest.fixture
def services():
    return FakeServices()


@pytest.fixture
def client(settings, services):
    with TestClient(create_app(settings, services=services), raise_server_exceptions=False) as connection:
        yield connection


def authenticate(client):
    response = client.post(BASE + "/auth/login", json={"invite_code": "test-only"})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def request(client, path, headers, *, method="POST", body=None, expected=200):
    response = client.request(method, BASE + path, headers=headers, json={} if body is None else body)
    assert response.status_code == expected, response.text
    return response.json()


def episode_state(client, episode_id):
    response = client.get(BASE + "/episodes/" + episode_id)
    assert response.status_code == 200, response.text
    return response.json()


def create_with_asset(client, content):
    auth = authenticate(client)
    episode = request(client, "/episodes", auth, body={"title": "本机流程测试", "background": "合成测试素材"}, expected=201)
    uploaded = client.post(BASE + f"/episodes/{episode['id']}/assets", headers=auth,
                          files={"file": ("synthetic-tone.mp4", content, "video/mp4")})
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["has_audio"] is True
    return auth, episode["id"], uploaded.json()


def advance(client, job_id, expected="succeeded"):
    Worker(client.app.state.repo, pipeline=client.app.state.pipeline).process_one()
    response = client.get(BASE + "/jobs/" + job_id)
    assert response.status_code == 200
    job = response.json()
    assert job["state"] == expected, job
    return job


def analyze_and_review(client, auth, episode_id):
    job = request(client, f"/episodes/{episode_id}/analysis", auth)
    advance(client, job["id"])
    before = episode_state(client, episode_id)
    assert before["analysis_state"] == "review"
    assert before["evidence_confirmed_revision"] is None
    assert {e["kind"] for e in before["evidence"]} == {"visual", "speech"}
    items = deepcopy(before["evidence"])
    items[0].update(start=.2, end=1.2, note="人工核对的测试画面：蓝色。")
    reviewed = request(client, f"/episodes/{episode_id}/evidence", auth, method="PUT",
        body={"revision": before["evidence_revision"], "items": items, "confirmed": True})
    assert reviewed["evidence_revision"] > before["evidence_revision"]
    assert reviewed["analysis_state"] == "confirmed"
    assert reviewed["evidence_confirmed_revision"] == reviewed["evidence_revision"]
    return reviewed


def select_proposal(client, auth, episode_id, packaging="standard"):
    job = request(client, f"/episodes/{episode_id}/directions", auth)
    advance(client, job["id"])
    episode = episode_state(client, episode_id)
    assert episode["directions_revision"] == episode["evidence_revision"]
    direction = episode["directions"][0]
    return request(client, f"/episodes/{episode_id}/direction", auth, expected=201,
        body={"direction_id": direction["id"], "revision": episode["directions_revision"], "packaging": packaging})


def render_confirm_export(client, auth, episode_id, version_id):
    queued = request(client, f"/versions/{version_id}/render", auth)
    job = advance(client, queued["id"])
    assert job["result"]["has_audio"] is True
    version = next(v for v in episode_state(client, episode_id)["versions"] if v["id"] == version_id)
    assert version["state"] == "ready"
    assert client.get(BASE + f"/versions/{version_id}/media").status_code == 200
    assert client.get(BASE + f"/versions/{version_id}/export").status_code == 409
    confirmed = request(client, f"/versions/{version_id}/confirm", auth)
    assert confirmed["state"] == "confirmed"
    exported = client.get(BASE + f"/versions/{version_id}/export")
    assert exported.status_code == 200 and len(exported.content) > 100
    return exported.content


def test_complete_review_direction_narration_render_export_and_immutable_revision(
        client, services, voiced_video_bytes):
    auth, eid, asset = create_with_asset(client, voiced_video_bytes)
    repo = client.app.state.repo
    with repo.factory() as db:
        source = repo.path(db.get(Asset, asset["id"]).path)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    reviewed = analyze_and_review(client, auth, eid)
    assert reviewed["analysis_coverage"][0]["asset_id"] == asset["id"]
    version = select_proposal(client, auth, eid)
    assert version["origin"] == "demo"  # injected fixtures must never claim real AI
    assert version["narration_text"] == "测试这一刻"
    assert version["narration_approved"] is False and version["narration_ready"] is False
    assert request(client, f"/versions/{version['id']}/render", auth, expected=409)["error"]["code"] == "NARRATION_REQUIRED"
    assert request(client, f"/versions/{version['id']}/narration", auth,
                   body={"confirm_text": False}, expected=409)["error"]["code"] == "CONFIRM_REQUIRED"
    assert services.counts["tts"] == 0
    voice_job = request(client, f"/versions/{version['id']}/narration", auth, body={"confirm_text": True})
    advance(client, voice_job["id"])
    voiced = next(v for v in episode_state(client, eid)["versions"] if v["id"] == version["id"])
    assert voiced["narration_approved"] is True and voiced["narration_ready"] is True
    exported = render_confirm_export(client, auth, eid, version["id"])
    with repo.factory() as db:
        old = db.get(Version, version["id"])
        output, old_confirmed = repo.path(old.output_path), old.confirmed_at
        old_segments, old_narration = deepcopy(old.segments), old.narration_text
    info = media.probe(output)
    assert (info["width"], info["height"]) == (1280, 720)
    assert 1.5 <= info["duration"] < 2 and info["has_audio"]
    media._run("ffmpeg", ["-nostdin", "-v", "error", "-xerror", "-threads", "2",
        "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov", "-enable_drefs", "0",
        "-i", str(output), "-f", "null", "-"], timeout=20, code="MEDIA_DECODE_FAILED")
    assert exported == output.read_bytes()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    edited_segments = deepcopy(old_segments)
    edited_segments[0]["end"] = .9
    edited_segments[0]["caption"] = "另存修改"
    new = request(client, f"/episodes/{eid}/versions", auth, expected=201, body={
        "title": "无旁白修改版", "segments": edited_segments, "base_version_id": version["id"],
        "narration_text": "", "packaging": "light"})
    assert new["id"] != version["id"] and new["base_version_id"] == version["id"]
    assert new["narration_ready"] is False and new["narration_approved"] is False
    with repo.factory() as db:
        old = db.get(Version, version["id"])
        assert old.state == "confirmed" and old.confirmed_at == old_confirmed
        assert old.segments == old_segments and old.narration_text == old_narration
        calls, ledgers = list(db.scalars(select(ModelCall))), list(db.scalars(select(Budget)))
        assert {call.kind for call in calls} == {"vision", "asr", "directions", "tts"}
        assert all(call.usage["real_call"] is False for call in calls)
        assert all(row.state == "settled" and row.cost is not None for row in ledgers)
        assert sum(row.cost for row in ledgers) == 30380
    assert services.counts == Counter(vision=1, asr=1, directions=1, tts=1)
    budget = episode_state(client, eid)["budget"]
    assert budget["spent"] == pytest.approx(.03038) and budget["reserved"] == 0
    render_confirm_export(client, auth, eid, new["id"])
    assert client.get(BASE + f"/versions/{version['id']}/export").content == exported


def test_default_production_services_refuse_cloud_without_reserving(settings, voiced_video_bytes):
    with TestClient(create_app(settings), raise_server_exceptions=False) as default:
        auth, eid, _ = create_with_asset(default, voiced_video_bytes)
        assert default.get(BASE + "/status").json()["cloud_enabled"] is False
        result = request(default, f"/episodes/{eid}/analysis", auth, expected=409)
        assert result["error"]["code"] == "CLOUD_DISABLED"
        with default.app.state.repo.factory() as db:
            assert not list(db.scalars(select(Job)))
            assert not list(db.scalars(select(Budget)))
            assert not list(db.scalars(select(ModelCall)))


def test_unconfirmed_and_forged_review_never_reaches_directions(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analysis = request(client, f"/episodes/{eid}/analysis", auth)
    advance(client, analysis["id"])
    episode = episode_state(client, eid)
    denied = request(client, f"/episodes/{eid}/directions", auth, expected=409)
    assert denied["error"]["code"] == "REVIEW_REQUIRED"
    for change in ({"end": 20}, {"asset_id": "not-owned"}, {"id": "invented"}, {"kind": "speech"}):
        items = deepcopy(episode["evidence"])
        items[0].update(change)
        result = request(client, f"/episodes/{eid}/evidence", auth, method="PUT", expected=422,
            body={"revision": episode["evidence_revision"], "items": items, "confirmed": True})
        assert result["error"]["code"] == "INVALID_EVIDENCE"
    denied = request(client, f"/episodes/{eid}/evidence", auth, method="PUT", expected=409,
        body={"revision": episode["evidence_revision"], "items": episode["evidence"], "confirmed": False})
    assert denied["error"]["code"] == "CONFIRM_REQUIRED"
    assert episode_state(client, eid)["evidence"] == episode["evidence"]
    assert services.counts["directions"] == 0


def test_out_of_bounds_model_direction_is_not_published(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    services.forge_direction = True
    job = request(client, f"/episodes/{eid}/directions", auth)
    failed = advance(client, job["id"], "failed")
    assert failed["error"] and episode_state(client, eid)["directions"] == []


@pytest.mark.parametrize("change", ["upload", "evidence"])
def test_source_or_evidence_change_invalidates_directions_and_confirmed_version(
        client, voiced_video_bytes, change):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    analyze_and_review(client, auth, eid)
    version = select_proposal(client, auth, eid, packaging="original")
    render_confirm_export(client, auth, eid, version["id"])
    before = episode_state(client, eid)
    if change == "upload":
        added = client.post(BASE + f"/episodes/{eid}/assets", headers=auth,
            files={"file": ("another.mp4", voiced_video_bytes, "video/mp4")})
        assert added.status_code == 201, added.text
    else:
        items = deepcopy(before["evidence"])
        items[0]["note"] = "人工修订后的新描述"
        request(client, f"/episodes/{eid}/evidence", auth, method="PUT", body={
            "revision": before["evidence_revision"], "items": items, "confirmed": True})
    after = episode_state(client, eid)
    assert after["evidence_revision"] > before["evidence_revision"]
    assert after["directions"] == [] and after["directions_revision"] is None
    old = next(v for v in after["versions"] if v["id"] == version["id"])
    assert old["state"] == "restricted"
    assert client.get(BASE + f"/versions/{version['id']}/export").status_code == 409
    assert request(client, f"/versions/{version['id']}/render", auth, expected=409)["error"]
    assert request(client, f"/episodes/{eid}/direction", auth, expected=409, body={
        "direction_id": "test-direction", "revision": before["directions_revision"], "packaging": "original"
    })["error"]["code"] == "STALE_REVISION"


def test_budget_over_limit_stops_before_adapter_invocation(client, services, voiced_video_bytes):
    auth, eid, _ = create_with_asset(client, voiced_video_bytes)
    repo = client.app.state.repo
    with repo.transaction() as db:
        identifier = repo.reserve(db, "owner", eid, 2.5)
        ledger = db.get(Budget, identifier)
        ledger.state, ledger.cost = "settled", 2_500_000
    job = request(client, f"/episodes/{eid}/analysis", auth)
    failed = advance(client, job["id"], "failed")
    assert failed["error"]["code"] == "BUDGET_EXCEEDED"
    assert services.counts == Counter()
    with repo.factory() as db:
        assert not list(db.scalars(select(ModelCall)))
    budget = episode_state(client, eid)["budget"]
    assert budget["spent"] == 2.5 and budget["reserved"] == 0


def test_unknown_fee_is_persisted_and_blocks_retry(settings, voiced_video_bytes):
    fake = FakeServices(failures={"vision": ["unknown"]})
    with TestClient(create_app(settings, services=fake), raise_server_exceptions=False) as client:
        auth, eid, _ = create_with_asset(client, voiced_video_bytes)
        job = request(client, f"/episodes/{eid}/analysis", auth)
        failed = advance(client, job["id"], "failed")
        assert failed["error"]["code"] == "MODEL_TIMEOUT"
        assert request(client, f"/jobs/{job['id']}/retry", auth, expected=409)["error"]["code"] == "COST_UNKNOWN"
        with client.app.state.repo.factory() as db:
            ledger = db.scalars(select(Budget)).one()
            assert ledger.state == "unknown" and ledger.cost is None and ledger.amount == 850000
        cookies = dict(client.cookies)
    with TestClient(create_app(settings, services=fake), raise_server_exceptions=False) as restarted:
        restarted.cookies.update(cookies)
        budget = episode_state(restarted, eid)["budget"]
        assert budget["spent"] == 0 and budget["reserved"] == .85
        assert request(restarted, f"/jobs/{job['id']}/retry", auth, expected=409)["error"]["code"] == "COST_UNKNOWN"
    assert fake.counts["vision"] == 1


def test_restart_reuses_vision_checkpoint_after_known_asr_failure(settings, voiced_video_bytes):
    fake = FakeServices(failures={"asr": ["known"]})
    with TestClient(create_app(settings, services=fake), raise_server_exceptions=False) as first:
        auth, eid, asset = create_with_asset(first, voiced_video_bytes)
        job = request(first, f"/episodes/{eid}/analysis", auth)
        failed = advance(first, job["id"], "failed")
        assert failed["error"]["code"] == "TEST_KNOWN_FAILURE"
        with first.app.state.repo.factory() as db:
            saved = db.get(Job, job["id"])
            assert "vision" in saved.payload["steps"]
            assert "asr:" + asset["id"] not in saved.payload["steps"]
        cookies = dict(first.cookies)
    with TestClient(create_app(settings, services=fake), raise_server_exceptions=False) as restarted:
        restarted.cookies.update(cookies)
        retried = request(restarted, f"/jobs/{job['id']}/retry", auth)
        assert retried["id"] == job["id"]
        advance(restarted, job["id"])
        after = episode_state(restarted, eid)
        assert after["analysis_state"] == "review"
        assert {e["kind"] for e in after["evidence"]} == {"visual", "speech"}
        with restarted.app.state.repo.factory() as db:
            assert len(list(db.scalars(select(ModelCall).where(ModelCall.kind == "vision")))) == 1
            assert len(list(db.scalars(select(ModelCall).where(ModelCall.kind == "asr")))) == 2
            assert all(c.usage["real_call"] is False for c in db.scalars(select(ModelCall)))
        assert not list((settings.data_dir / "incoming").iterdir())
    assert fake.counts == Counter(vision=1, asr=2)
    assert fake.preparations["vision"] == 1
