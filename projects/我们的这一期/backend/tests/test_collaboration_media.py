"""Real local FFmpeg collaboration flow using generated, non-personal fixtures."""
from pathlib import Path
import shutil
import subprocess
import wave

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.collaboration_models import Submission
from app.config import Settings
from app.main import create_app
from app.models import Asset, Job, uid
from app.services.media import probe
from app.worker import Worker


@pytest.fixture(scope="module")
def generated_media(tmp_path_factory):
    root = tmp_path_factory.mktemp("nonpersonal-collaboration-media")
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not Path(ffmpeg).exists():
        pytest.skip("FFmpeg must be installed for real local media validation")
    video = root / "generated.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "color=c=navy:s=96x64:r=30:d=1.2", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.2",
                    "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", "-movflags", "+faststart", str(video)], check=True, capture_output=True, timeout=30)
    audio = root / "generated.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        # A deterministic nonspeech signal; this is not a synthesized interview.
        output.writeframes(b"\x00\x10\x00\xf0" * 14400)
    return video, audio


@pytest.mark.parametrize("kind", ["audio", "video", "contribution"])
def test_real_local_upload_preview_personal_consent_adoption_render_and_withdrawal(tmp_path, generated_media, kind):
    video, audio = generated_media
    app = create_app(Settings(data_dir=tmp_path / "isolated-real-media", invites={"owner-test": "owner"}, worker_enabled=False))
    with TestClient(app) as client:
        auth = client.post("/api/v1/auth/login", json={"invite_code": "owner-test"}).json()
        owner = {"X-CSRF-Token": auth["csrf_token"]}
        episode_id = client.post("/api/v1/episodes", headers=owner, json={"title": "本机合成素材验证"}).json()["id"]
        uploaded = client.post(f"/api/v1/episodes/{episode_id}/assets", headers=owner,
                               files={"file": (video.name, video.read_bytes(), "video/mp4")})
        assert uploaded.status_code == 201, uploaded.text
        source_id = uploaded.json()["id"]
        invitation = client.post(f"/api/v1/episodes/{episode_id}/collaboration/invitations", headers=owner,
            json={"role": "contribute" if kind == "contribution" else "interview", "label": "测试本人", "context": {"asset_id": source_id, "start": .1, "end": .5},
                  "questions": ["请核对测试片段"]}).json()
        person = client.post("/api/v1/participant/auth/login", json={"invite_code": invitation["invite_code"], "display_name": "测试本人"}).json()
        participant = {"X-CSRF-Token": person["csrf_token"]}
        fixture = audio if kind == "audio" else video
        response = client.post("/api/v1/participant/submissions/upload", headers=participant,
            data={"kind": "video" if kind == "contribution" else kind, "client_request_id": uid(), "text": "这是合成本机测试素材"},
            files={"file": (fixture.name, fixture.read_bytes(), "application/octet-stream")})
        assert response.status_code == 201, response.text
        submission_id = response.json()["id"]
        worker = Worker(app.state.repo)
        worker.process_one()  # Context preview.
        worker.process_one()  # Holder's own original preview.
        with app.state.repo.factory() as db:
            jobs = list(db.scalars(select(Job)))
            assert all(job.state == "succeeded" for job in jobs), [(job.kind, job.error) for job in jobs]
            row = db.get(Submission, submission_id)
            original = app.state.repo.path(row.source_path)
            preview = app.state.repo.path(row.preview_path)
            assert original.read_bytes() == fixture.read_bytes()
            info = probe(preview)
            assert info["width"] == 1280 and info["height"] == 720 and info["has_audio"]
        response = client.post(f"/api/v1/participant/submissions/{submission_id}/submit", headers=participant, json={"confirmed": True})
        assert response.status_code == 200, response.text
        revision_id = response.json()["revisions"][0]["id"]
        adoption = client.post(f"/api/v1/collaboration/revisions/{revision_id}/adopt", headers=owner, json={})
        assert adoption.status_code == 200, adoption.text
        worker.process_one()
        with app.state.repo.factory() as db:
            job = db.get(Job, adoption.json()["id"])
            assert job.state == "succeeded", job.error
            asset = db.get(Asset, job.result["asset_id"])
            asset_id, duration = asset.id, asset.duration
            adopted_path = app.state.repo.path(asset.path)
            assert probe(adopted_path)["has_audio"]
        created = client.post(f"/api/v1/episodes/{episode_id}/versions", headers=owner,
            json={"title": "采访采用测试", "packaging": "original", "segments": [
                {"asset_id": asset_id, "start": 0, "end": duration, "caption": ""}]})
        assert created.status_code == 201, created.text
        version_id = created.json()["id"]
        rendered = client.post(f"/api/v1/versions/{version_id}/render", headers=owner, json={})
        assert rendered.status_code == 200, rendered.text
        worker.process_one()
        response = client.get(f"/api/v1/jobs/{rendered.json()['id']}")
        assert response.json()["state"] == "succeeded", response.text
        assert client.post(f"/api/v1/versions/{version_id}/confirm", headers=owner, json={}).status_code == 200
        assert client.get(f"/api/v1/versions/{version_id}/export").status_code == 200
        response = client.post(f"/api/v1/participant/submissions/{submission_id}/withdraw", headers=participant, json={"confirmed": True})
        assert response.status_code == 200
        assert client.get(f"/api/v1/versions/{version_id}/media").status_code == 409
        assert client.get(f"/api/v1/versions/{version_id}/export").status_code == 409
        assert original.exists() and adopted_path.exists()
