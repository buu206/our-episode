"""Explicit offline demo tests: fixture labels, zero spend, and real local flow."""
import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import Budget, ModelCall
from app.services import media
from app.services.director import DirectorError
from app.services.pipeline import ModelServices
from app.worker import Worker

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from demo_provider import DemoPreparedVideo, DemoServices


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("The demo must not contact a model or network")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(httpx.AsyncClient, "send", denied)


@pytest.mark.parametrize("kind", ["vision", "asr", "directions", "tts"])
def test_each_async_step_requires_permit_before_result_or_audio(tmp_path, kind):
    service = DemoServices()
    calls = []
    def permit(*args):
        calls.append(args)
        return False
    output = tmp_path / "not-created.wav"
    operations = {
        "vision": lambda: service.vision(DemoPreparedVideo((("one", 1.0),)), "r", permit),
        "asr": lambda: service.asr([{"asset_id": "one", "start": 0.0, "end": 1.0}], "r", permit),
        "directions": lambda: service.directions([], {"one": 1.0}, "", "r", permit),
        "tts": lambda: service.tts("never spoken", output, "r", permit),
    }
    with pytest.raises(DirectorError, match="本地任务许可"):
        asyncio.run(operations[kind]())
    assert calls and calls[0][0:2] == ("r", 0.01)
    assert not output.exists()


def test_fixed_results_are_labelled_bounded_and_zero_cost(tmp_path, monkeypatch):
    service = DemoServices()
    assert service.is_real is False
    assert service.ready() == (True, "流程演示：固定测试结果，不调用AI、不外发、不扣费")
    public = {name for name in vars(ModelServices) if not name.startswith("_") and callable(getattr(ModelServices, name))}
    assert all(callable(getattr(service, name)) for name in public)
    file = tmp_path / "do-not-invent-a-party.mp4"
    file.write_bytes(b"probe-is-stubbed-for-unit-test")
    monkeypatch.setattr(media, "probe", lambda path: {"duration": 0.6})
    prepared = service.prepare_vision([{"asset_id": "one", "path": file}], tmp_path)
    permits = []
    def permit(*args):
        permits.append(args)
        return True
    vision = asyncio.run(service.vision(prepared, "r", permit))
    assert permits[-1] == ("r", .01, prepared.request_digest)
    observations = vision["result"]["observations"]
    assert len(observations) == 2
    assert all(0 <= item["time"] < .6 and "演示占位，未进行画面理解" in item["note"] for item in observations)
    assert "do-not-invent" not in json.dumps(vision)
    assert vision["result"]["coverage"][0]["sample_times"] == []
    chunks = service.prepare_audio(SimpleNamespace(id="one", duration=.6), file, tmp_path)
    asr = asyncio.run(service.asr(chunks, "r", permit))
    assert all("非真实转写" in row["text"] and 0 <= row["start"] < row["end"] <= .6 for row in asr["result"])
    evidence = [{"asset_id": "one", "start": .1, "end": .4, "note": "invented party to ignore"}]
    directions = asyncio.run(service.directions(evidence, {"one": .6}, "invented party to ignore", "r", permit))
    choices = directions["result"].model_dump()["directions"]
    assert len(choices) == 2
    assert all("演示" in choice["title"] and choice["evidence"][0]["start"] == .1 for choice in choices)
    assert "invented party" not in json.dumps(choices)
    output = tmp_path / "tone.wav"
    tts = asyncio.run(service.tts("这句话不会被朗读", output, "r", permit))
    with wave.open(str(output)) as audio:
        assert audio.getnframes() / audio.getframerate() == 1
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        assert any(audio.readframes(audio.getnframes()))
    for response in [vision, asr, directions, tts]:
        assert response["usage"]["cost_yuan"] == 0
        assert response["usage"]["real_call"] is False
        assert response["usage"]["model"] == "DEMO-FIXTURE"


def test_demo_entrypoint_does_not_load_env_or_start_normal_app():
    code = """
import runpy, sys
from pathlib import Path
scripts = Path(sys.argv[1])
sys.path.insert(0, str(scripts))
sys.path.insert(0, str(scripts.parent / 'backend'))
import app.config
def forbidden(*args, **kwargs):
    raise AssertionError('demo attempted to read production environment')
app.config.load_dotenv = forbidden
app.config.Settings.from_env = forbidden
namespace = runpy.run_path(str(scripts / 'demo-server.py'), run_name='demo_entry_test')
app = namespace['build_app']()
assert app.state.settings.cloud == {}
assert app.state.settings.data_dir == scripts.parent / 'data' / 'demo'
assert app.state.settings.invites == {'demo': 'demo-viewer'}
assert not hasattr(app.state, 'repo')
"""
    subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], check=True, capture_output=True, timeout=15)


def test_demo_full_flow_persists_fixture_labels_and_zero_spend(tmp_path):
    video = tmp_path / "synthetic.mp4"
    subprocess.run([media._binary("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=64x64:r=10:d=1", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000:duration=1", "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video)],
        check=True, capture_output=True, timeout=30)
    app = create_app(Settings(data_dir=tmp_path / "demo", invites={"demo": "demo-viewer"},
                              worker_enabled=False, cloud={}), services=DemoServices())
    with TestClient(app) as client:
        status = client.get("/api/v1/status").json()
        assert status["cloud_enabled"] is False
        assert status["workflow_enabled"] is True and status["model_mode"] == "demo"
        login = client.post("/api/v1/auth/login", json={"invite_code": "demo"})
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        def post(path, body=None, expected=200):
            response = client.post("/api/v1" + path, json=body or {})
            assert response.status_code == expected, response.text
            return response.json()
        episode = post("/episodes", {"title": "固定演示测试"}, 201)
        eid = episode["id"]
        uploaded = client.post(f"/api/v1/episodes/{eid}/assets",
                               files={"file": ("synthetic.mp4", video.read_bytes(), "video/mp4")})
        assert uploaded.status_code == 201
        def get_episode():
            return client.get(f"/api/v1/episodes/{eid}").json()
        def finish(job):
            Worker(app.state.repo, pipeline=app.state.pipeline).process_one()
            result = client.get("/api/v1/jobs/" + job["id"]).json()
            assert result["state"] == "succeeded", result
        finish(post(f"/episodes/{eid}/analysis"))
        current = get_episode()
        assert len(current["evidence"]) == 3
        assert all("演示占位" in row["note"] for row in current["evidence"])
        post(f"/episodes/{eid}/directions", expected=409)
        response = client.put(f"/api/v1/episodes/{eid}/evidence", json={
            "revision": current["evidence_revision"], "items": current["evidence"], "confirmed": True})
        assert response.status_code == 200, response.text
        finish(post(f"/episodes/{eid}/directions"))
        current = get_episode()
        assert len(current["directions"]) == 2
        version = post(f"/episodes/{eid}/direction", {"direction_id": current["directions"][0]["id"],
                       "revision": current["directions_revision"], "packaging": "standard"}, 201)
        assert version["origin"] == "demo"
        post("/versions/" + version["id"] + "/narration", {"confirm_text": False}, 409)
        finish(post("/versions/" + version["id"] + "/narration", {"confirm_text": True}))
        finish(post("/versions/" + version["id"] + "/render"))
        post("/versions/" + version["id"] + "/confirm")
        exported = client.get("/api/v1/versions/" + version["id"] + "/export")
        assert exported.status_code == 200 and exported.content
        current = get_episode()
        assert current["budget"]["spent"] == current["budget"]["reserved"] == 0
        with app.state.repo.factory() as db:
            calls = list(db.scalars(select(ModelCall)))
            assert {call.kind for call in calls} == {"vision", "asr", "directions", "tts"}
            assert all(call.usage["real_call"] is False and call.usage["model"] == "DEMO-FIXTURE" for call in calls)
            assert all(row.cost == 0 and row.state == "settled" for row in db.scalars(select(Budget)))
