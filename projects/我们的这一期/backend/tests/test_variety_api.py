"""Packaging-only revisions reuse exactly approved narration without a model call."""

from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import Episode, Job, Version
from app.worker import Worker
from test_api import (settings, client, headers, episode, asset, video_bytes,
                      create_version, mark_ready, assert_error, BASE)
from test_packaging import narration


@pytest.fixture
def voiced_base(client, headers, episode, asset, video_bytes):
    response = create_version(client, headers, episode["id"], asset["id"],
                              narration_text="在场，开拍！", packaging="standard")
    assert response.status_code == 201
    base = response.json()
    mark_ready(client, base["id"], video_bytes)
    repo = client.app.state.repo
    relative = f"outputs/{base['id']}.wav"
    narration(repo.path(relative), active=.4, lead=0, tail=0)
    with repo.transaction() as db:
        saved = db.get(Version, base["id"])
        saved.narration_path, saved.narration_approved = relative, True
    return base


def revised(client, headers, episode, base, **changes):
    body = {"title": "综艺包装版", "segments": deepcopy(base["segments"]),
            "base_version_id": base["id"], "narration_text": base["narration_text"], "packaging": "variety"}
    body.update(changes)
    return client.post(BASE + f"/episodes/{episode['id']}/versions", headers=headers, json=body)


@pytest.mark.parametrize("packaging", ["variety", "variety_v2"])
def test_packaging_only_revision_reuses_audio_and_renders_locally(client, headers, episode, voiced_base, packaging):
    response = revised(client, headers, episode, voiced_base, packaging=packaging)
    assert response.status_code == 201, response.text
    new = response.json()
    assert new["packaging"] == packaging and new["state"] == "draft"
    assert new["narration_ready"] and new["narration_approved"]
    repo = client.app.state.repo
    with repo.factory() as db:
        old, saved = db.get(Version, voiced_base["id"]), db.get(Version, new["id"])
        assert old.narration_path == saved.narration_path
        assert old.state == "ready" and saved.output_path is None and saved.confirmed_at is None
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'narration')) == 0
    queued = client.post(BASE + f"/versions/{new['id']}/render", headers=headers)
    assert queued.status_code == 200
    Worker(repo).process_one()
    with repo.factory() as db:
        saved, old = db.get(Version, new["id"]), db.get(Version, voiced_base["id"])
        assert saved.state == "ready" and saved.confirmed_at is None
        assert saved.output_path != old.output_path and repo.path(old.output_path).is_file()
        assert db.get(Job, queued.json()["id"]).result["packaging"] == packaging
    assert_error(client.get(BASE + f"/versions/{new['id']}/export"), 409)


def test_reused_audio_cannot_enqueue_a_second_paid_narration(client, headers, episode, voiced_base, monkeypatch):
    new = revised(client, headers, episode, voiced_base).json()
    monkeypatch.setattr("app.main.Pipeline.require_ready", lambda self: None)
    response = client.post(BASE + f"/versions/{new['id']}/narration", headers=headers, json={"confirm_text": True})
    assert assert_error(response, 409)["code"] == "NARRATION_READY"
    with client.app.state.repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == 'narration')) == 0


@pytest.mark.parametrize("change", ["text", "span", "caption", "state", "approval"])
def test_changed_content_or_unready_base_never_inherits_voice_approval(client, headers, episode, voiced_base, change):
    changes = {}
    if change == "text":
        changes["narration_text"] = "改过的旁白"
    elif change in ("span", "caption"):
        changes["segments"] = deepcopy(voiced_base["segments"])
        changes["segments"][0]["start" if change == "span" else "caption"] = .1 if change == "span" else "改过花字"
    else:
        with client.app.state.repo.transaction() as db:
            base = db.get(Version, voiced_base["id"])
            if change == "state":
                base.state = "failed"
            else:
                base.narration_approved = False
    response = revised(client, headers, episode, voiced_base, **changes)
    assert response.status_code == 201
    assert not response.json()["narration_ready"] and not response.json()["narration_approved"]


@pytest.mark.parametrize("change", ["missing", "symlink", "outside_outputs", "silence"])
def test_unusable_audio_cannot_be_reused(client, headers, episode, voiced_base, change, tmp_path):
    repo = client.app.state.repo
    with repo.transaction() as db:
        base = db.get(Version, voiced_base["id"])
        path = repo.path(base.narration_path)
        if change == "missing":
            path.unlink()
        elif change == "symlink":
            target = tmp_path / "outside.wav"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
        elif change == "outside_outputs":
            base.narration_path = "assets/" + path.name
        else:
            narration(path, active=0, lead=.4, tail=0)
    response = revised(client, headers, episode, voiced_base)
    assert_error(response, 409)
    with repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Version)) == 1


def test_stale_ai_evidence_prevents_reuse(client, headers, episode, voiced_base):
    repo = client.app.state.repo
    with repo.transaction() as db:
        base = db.get(Version, voiced_base["id"])
        base.origin, base.evidence_revision = "ai", 1
        ep = db.get(Episode, episode["id"])
        ep.evidence_revision = ep.evidence_confirmed_revision = 2
    response = revised(client, headers, episode, voiced_base)
    assert assert_error(response, 409)["code"] == "EVIDENCE_CHANGED"


def test_deleting_shared_source_revokes_both_versions_and_audio(client, headers, episode, asset, voiced_base):
    new = revised(client, headers, episode, voiced_base).json()
    repo = client.app.state.repo
    with repo.factory() as db:
        voice = repo.path(db.get(Version, new["id"]).narration_path)
    result = client.delete(BASE + f"/assets/{asset['id']}?confirm=true", headers=headers)
    assert result.status_code == 200, result.text
    for version_id in (voiced_base["id"], new["id"]):
        assert_error(client.post(BASE + f"/versions/{version_id}/render", headers=headers), 409)
    assert not voice.exists()
