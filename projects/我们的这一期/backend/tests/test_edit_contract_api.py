"""New edit metadata crosses HTTP/storage/worker boundaries without cloud calls.

Evidence is explicitly seeded test data. The interview harness uses offline
media helpers; these tests make no claim about model or rendered story quality.
"""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.main import create_app
from app.models import Episode, Job, Version
from app.repository import DomainError
from app.schemas import Segment
from app.services.edit_contract import is_director_edit
from app.worker import source_segments
from test_api import (settings, client, headers, episode, asset, video_bytes,
                      BASE, assert_error, get_episode, login)
from test_collaboration import h
from test_variety_api import voiced_base, revised


@pytest.fixture
def reviewed(client, episode, asset):
    """A half-second real technical clip with deliberately synthetic evidence."""
    rows = [
        {"id": "visual-test", "asset_id": asset["id"], "start": 0., "end": .5,
         "kind": "visual", "note": "Synthetic blue-frame observation."},
        {"id": "speech-test", "asset_id": asset["id"], "start": .18, "end": .30,
         "kind": "speech", "note": "Synthetic utterance; source is not actual speech."},
    ]
    with client.app.state.repo.transaction() as db:
        saved = db.get(Episode, episode["id"])
        saved.evidence = rows
        saved.evidence_revision = saved.evidence_confirmed_revision = 7
        saved.analysis_asset_ids = [asset["id"]]
        saved.analysis_state = "confirmed"
    return rows


def planned_segments(asset_id):
    first = {"asset_id": asset_id, "start": 0., "end": .5, "caption": "",
             "purpose": "Introduce the existing scene", "evidence_ids": ["visual-test", "speech-test"],
             "transition": "cut", "transition_duration": 0.}
    second = {**first, "purpose": "Return to the established scene", "transition": "dissolve",
              "transition_duration": .1}
    return [first, second]


def save_plan(client, headers, episode_id, segments, **extra):
    return client.post(BASE + f"/episodes/{episode_id}/versions", headers=headers,
                       json={"title": "Synthetic contract regression", "segments": segments,
                             "packaging": "original", **extra})


def normalized(segments):
    return [Segment.model_validate(item).model_dump() for item in segments]


def test_typed_http_plan_preserves_metadata_through_restart_and_worker(
        client, settings, headers, episode, asset, reviewed):
    plan = planned_segments(asset["id"])
    response = save_plan(client, headers, episode["id"], plan)
    assert response.status_code == 201, response.text
    result = response.json()
    assert result["segments"] == normalized(plan)
    assert result["evidence_revision"] == 7
    with client.app.state.repo.factory() as db:
        version = db.get(Version, result["id"])
        assert is_director_edit(db, version)
        assert normalized(version.segments) == normalized(plan)

    # New application/repository using this test's disk only; the worker is off.
    with TestClient(create_app(settings), raise_server_exceptions=False) as restarted:
        login(restarted)
        saved = next(v for v in get_episode(restarted, episode["id"])["versions"]
                     if v["id"] == result["id"])
        assert saved["segments"] == normalized(plan)
        repo = restarted.app.state.repo
        with repo.factory() as db:
            version = db.get(Version, result["id"])
            assert is_director_edit(db, version)
            actual = source_segments(repo, db, version)
            assert all(item["path"].is_file() for item in actual)
            clean = [{key: value for key, value in item.items()
                      if key not in ("path", "_preserve_expression")} for item in actual]
            assert normalized(clean) == normalized(plan)


@pytest.mark.parametrize("change", ["unknown_id", "wrong_asset_id", "trimmed_utterance",
                                    "incoming_over_speech", "outgoing_over_speech"])
def test_direct_http_save_rejects_invalid_provenance_and_speech_edits(
        client, headers, episode, asset, reviewed, change):
    plan = planned_segments(asset["id"])
    if change == "unknown_id":
        plan[0]["evidence_ids"] = ["not-current-evidence"]
    elif change == "wrong_asset_id":
        # A legitimate-looking ID from a different source must not be accepted.
        with client.app.state.repo.transaction() as db:
            saved = db.get(Episode, episode["id"])
            saved.evidence = [*saved.evidence, {**reviewed[0], "id": "other-source",
                                               "asset_id": "unrelated-asset"}]
        plan[0]["evidence_ids"] = ["other-source"]
    elif change == "trimmed_utterance":
        plan[0]["start"] = .24
    else:
        with client.app.state.repo.transaction() as db:
            saved = db.get(Episode, episode["id"])
            speech = dict(reviewed[1])
            speech.update({"start": .02, "end": .15} if change == "incoming_over_speech"
                          else {"start": .35, "end": .48})
            saved.evidence = [reviewed[0], speech]
    response = save_plan(client, headers, episode["id"], plan)
    assert assert_error(response, 422)["code"] == "INVALID_DIRECTOR_EDIT"
    assert get_episode(client, episode["id"])["versions"] == []
    with client.app.state.repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "render")) == 0


@pytest.mark.parametrize("base_creation", ["direct_save", "select_direction"])
def test_derived_version_cannot_clear_metadata_to_escape_the_contract(
        client, headers, episode, asset, reviewed, base_creation):
    plan = planned_segments(asset["id"])
    if base_creation == "direct_save":
        response = save_plan(client, headers, episode["id"], plan)
    else:
        # Selection uses an offline stored proposal, not a real model response.
        with client.app.state.repo.transaction() as db:
            saved = db.get(Episode, episode["id"])
            saved.directions_revision = 7
            saved.directions = [{"id": "test_direction", "title": "Synthetic planned direction",
                                 "summary": "Explicit offline selection fixture.",
                                 "evidence": [{key: reviewed[0][key]
                                               for key in ("asset_id", "start", "end", "note")}],
                                 "edit_plan": {"segments": plan, "narration_text": ""}}]
        response = client.post(BASE + f"/episodes/{episode['id']}/direction", headers=headers,
                               json={"revision": 7, "direction_id": "test_direction", "packaging": "original"})
    assert response.status_code == 201, response.text
    base = response.json()
    assert base["segments"] == normalized(plan)
    with client.app.state.repo.factory() as db:
        assert is_director_edit(db, db.get(Version, base["id"]))
    stripped = [{"asset_id": asset["id"], "start": .24, "end": .5, "caption": ""}]
    response = save_plan(client, headers, episode["id"], stripped, base_version_id=base["id"])
    assert assert_error(response, 422)["code"] == "INVALID_DIRECTOR_EDIT"
    saved = get_episode(client, episode["id"])["versions"]
    assert [row["id"] for row in saved] == [base["id"]]
    assert saved[0]["segments"] == base["segments"]


def test_worker_rechecks_marked_plan_even_if_persisted_metadata_is_cleared(
        client, headers, episode, asset, reviewed):
    response = save_plan(client, headers, episode["id"], planned_segments(asset["id"]))
    assert response.status_code == 201, response.text
    repo = client.app.state.repo
    with repo.transaction() as db:
        saved = db.get(Version, response.json()["id"])
        saved.segments = [{"asset_id": asset["id"], "start": 0., "end": .5, "caption": ""}]
    with repo.factory() as db:
        with pytest.raises(DomainError) as error:
            source_segments(repo, db, db.get(Version, response.json()["id"]))
        assert error.value.code == "INVALID_DIRECTOR_EDIT"


def test_legacy_confirmed_version_without_defaults_still_reuses_approved_narration(
        client, headers, episode, voiced_base):
    repo = client.app.state.repo
    legacy_fields = ("asset_id", "start", "end", "caption")
    with repo.transaction() as db:
        base = db.get(Version, voiced_base["id"])
        base.segments = [{key: item[key] for key in legacy_fields} for item in base.segments]
    confirmed = client.post(BASE + f"/versions/{voiced_base['id']}/confirm", headers=headers, json={})
    assert confirmed.status_code == 200, confirmed.text
    # A newer client sends explicit defaults even though the old stored JSON lacks them.
    response = revised(client, headers, episode, voiced_base,
                       segments=deepcopy(confirmed.json()["segments"]))
    assert response.status_code == 201, response.text
    assert response.json()["narration_ready"] and response.json()["narration_approved"]
    with repo.factory() as db:
        base, new = db.get(Version, voiced_base["id"]), db.get(Version, response.json()["id"])
        assert base.state == "confirmed" and new.state == "draft"
        assert base.narration_path == new.narration_path
        assert set(base.segments[0]) == set(legacy_fields)
        assert not is_director_edit(db, base) and not is_director_edit(db, new)
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "narration")) == 0


@pytest.mark.parametrize("protected_position", [0, 1])
def test_confirmed_interview_cannot_be_hidden_by_either_adjacent_transition(h, protected_position):
    _, _, submission = h.submitted("interview")
    adopted = h.adopt(submission)
    protected = {"asset_id": adopted, "start": 0., "end": 3., "caption": ""}
    ordinary = {"asset_id": h.source, "start": 0., "end": 3., "caption": ""}
    plan = [protected, ordinary] if protected_position == 0 else [ordinary, protected]
    plan[1] = {**plan[1], "transition": "dissolve", "transition_duration": .2}
    response = save_plan(h.client, h.owner, h.episode, plan)
    assert assert_error(response, 422)["code"] == "INVALID_TRANSITION"
    assert get_episode(h.client, h.episode)["versions"] == []
    # Worker must also reject a stored plan, not rely solely on the HTTP gate.
    with h.repo.transaction() as db:
        version = Version(owner_id="alice", episode_id=h.episode, title="Offline protected test",
                          segments=plan, origin="manual", packaging="original")
        db.add(version)
        db.flush()
        version_id = version.id
    with h.repo.factory() as db:
        with pytest.raises(DomainError) as error:
            source_segments(h.repo, db, db.get(Version, version_id))
        assert error.value.code == "INVALID_TRANSITION"
def test_legacy_manual_short_span_remains_readable():
    from app.schemas import Segment
    assert Segment.model_validate({'asset_id': 'legacy', 'start': 0., 'end': .05, 'caption': ''}).end == .05

