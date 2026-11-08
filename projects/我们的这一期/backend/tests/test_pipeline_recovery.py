"""Offline regressions for admission, charged results and crash/retry boundaries.

Fakes exist only in this test module. No network client, model or real Key is used.
"""

import wave

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.models import Asset, Budget, Episode, Job, ModelCall, Version
from app.repository import DomainError, Repository
from app.services.pipeline import Pipeline


class OfflineServices:
    is_real = False

    def ready(self):
        return True, "offline test dependency only"


@pytest.fixture
def repo(tmp_path):
    instance = Repository(tmp_path / "isolated-pipeline")
    yield instance
    instance.engine.dispose()


def seed(repo, *, kind="analysis", state="running", owner="alice"):
    path = repo.path("assets/test.mp4")
    path.write_bytes(b"offline-test-reference")
    with repo.transaction() as db:
        episode = Episode(owner_id=owner, title="恢复边界", evidence_revision=1,
                          evidence_confirmed_revision=1, analysis_state="confirmed")
        db.add(episode)
        db.flush()
        asset = Asset(owner_id=owner, episode_id=episode.id, filename="test.mp4", path="assets/test.mp4",
                      size=path.stat().st_size, duration=10, width=1280, height=720, has_audio=False)
        db.add(asset)
        db.flush()
        episode.analysis_asset_ids = [asset.id]
        episode.evidence = [{"id": "evidence-1", "asset_id": asset.id, "start": 0, "end": 1,
                             "note": "用户核对的事实", "kind": "visual"}]
        job = Job(owner_id=owner, episode_id=episode.id, kind=kind, state=state, idempotency_key="test-job",
                  payload={"asset_ids": [asset.id], "revision": 1, "steps": {}})
        db.add(job)
        db.flush()
        return episode.id, asset.id, job.id


def call_record(repo, job_id, *, call_state="sending", bill_state="reserved", cost=None):
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        bid = repo.reserve(db, job.owner_id, job.episode_id, 0.2)
        bill = db.get(Budget, bid)
        bill.state, bill.cost = bill_state, cost
        call = ModelCall(owner_id=job.owner_id, episode_id=job.episode_id, job_id=job.id,
                         reservation_id=bid, kind="vision", state=call_state)
        db.add(call)
        db.flush()
        job.payload = {**job.payload, "reservation": bid}
        return bid, call.id


def test_recovery_distinguishes_prepared_from_possibly_sent(repo):
    _, _, job_id = seed(repo)
    prepared, _ = call_record(repo, job_id, call_state="prepared")
    sending, _ = call_record(repo, job_id, call_state="sending")
    repo.recover()
    with repo.factory() as db:
        assert db.get(Job, job_id).state == "failed"
        assert (db.get(Budget, prepared).state, db.get(Budget, prepared).cost) == ("settled", 0)
        assert db.get(Budget, sending).state == "unknown"
        assert db.get(Budget, sending).cost is None


def test_analysis_rejects_revision_changed_by_human_review(repo):
    episode_id, _, job_id = seed(repo)
    with repo.transaction() as db:
        episode = db.get(Episode, episode_id)
        episode.evidence_revision = 2
        episode.evidence_confirmed_revision = 2
    with repo.factory() as db, pytest.raises(DomainError) as error:
        Pipeline(repo, OfflineServices())._validate_job(db, db.get(Job, job_id))
    assert error.value.code in ("EVIDENCE_CHANGED", "STALE_REVISION")


def test_paid_result_survives_crash_before_callers_checkpoint_without_rebilling(repo):
    _, _, job_id = seed(repo)
    calls = []
    async def operation(reservation, permit):
        assert permit(reservation) is True
        calls.append(reservation)
        return {"result": {"observations": [], "uncertainties": ["没有可核对事件"]},
                "usage": {"cost_yuan": 0.05, "cost_status": "known", "model": "offline-test"}}
    pipeline = Pipeline(repo, OfflineServices())
    first = pipeline.invoke(job_id, "vision", 0.2, operation, request_digest="unchanged-input", checkpoint_key="vision")
    # Simulate death immediately after invoke has committed billing but before
    # analyze() has called checkpoint(); this is a genuine persistence boundary.
    repo.recover()
    with repo.transaction() as db:
        db.get(Job, job_id).state = "running"
    second = Pipeline(repo, OfflineServices()).invoke(job_id, "vision", 0.2, operation,
                                                     request_digest="unchanged-input", checkpoint_key="vision")
    assert second == first
    assert len(calls) == 1, "an already billed successful result must be durably reusable"


def test_cached_result_is_not_reused_after_revision_changes(repo):
    episode_id, _, job_id = seed(repo)
    with repo.transaction() as db:
        db.get(Job, job_id).payload = {"asset_ids": db.get(Job, job_id).payload["asset_ids"],
                                      "revision": 1, "steps": {"vision": {"observations": []}}}
        db.get(Episode, episode_id).evidence_revision = 2
    with pytest.raises(DomainError) as error:
        Pipeline(repo, OfflineServices()).invoke(job_id, "vision", 0.2, lambda *_: pytest.fail("new call"),
                                                 checkpoint_key="vision")
    assert error.value.code in ("EVIDENCE_CHANGED", "STALE_REVISION")


def test_unknown_cost_stops_next_paid_step_inside_the_same_running_job(repo):
    _, _, job_id = seed(repo)
    async def uncertain(reservation, permit):
        assert permit(reservation)
        return {"result": {"observations": []}, "usage": {"cost_yuan": None, "cost_status": "unknown"}}
    pipeline = Pipeline(repo, OfflineServices())
    try:
        pipeline.invoke(job_id, "vision", 0.2, uncertain, checkpoint_key="vision")
    except DomainError as error:
        assert error.code == "COST_UNKNOWN"
    async def must_not_send(*_):
        pytest.fail("a second paid operation ran while the previous cost was unresolved")
    with pytest.raises(DomainError) as error:
        pipeline.invoke(job_id, "asr", 0.1, must_not_send, checkpoint_key="asr:next")
    assert error.value.code == "COST_UNKNOWN"


def test_cross_owner_job_is_rejected_before_reserving_budget(repo):
    _, _, job_id = seed(repo)
    with repo.transaction() as db:
        db.get(Job, job_id).owner_id = "bob"
    with pytest.raises(DomainError) as error:
        Pipeline(repo, OfflineServices()).invoke(job_id, "vision", 0.2, lambda *_: pytest.fail("called"))
    assert error.value.code == "NOT_FOUND"
    with repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Budget)) == 0


def test_deletion_after_send_blocks_checkpoint_but_keeps_known_charge(repo):
    _, asset_id, job_id = seed(repo)
    async def operation(reservation, permit):
        assert permit(reservation)
        with repo.transaction() as db:
            db.get(Asset, asset_id).deleted = True
        return {"result": {"observations": []}, "usage": {"cost_yuan": 0.05, "cost_status": "known"}}
    pipeline = Pipeline(repo, OfflineServices())
    result = pipeline.invoke(job_id, "vision", 0.2, operation)
    with pytest.raises(DomainError) as error:
        pipeline.checkpoint(job_id, "vision", result)
    assert error.value.code == "SOURCES_CHANGED"
    with repo.factory() as db:
        bill = db.scalars(select(Budget)).one()
        assert bill.state == "settled" and bill.cost == 50_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    from app.main import create_app
    settings = Settings(data_dir=tmp_path / "http-recovery", invites={"test-first": "alice", "test-second": "bob"},
                        worker_enabled=False)
    app = create_app(settings, services=OfflineServices())
    with TestClient(app) as connection:
        auth = connection.post("/api/v1/auth/login", json={"invite_code": "test-first"})
        assert auth.status_code == 200
        connection.headers["X-CSRF-Token"] = auth.json()["csrf_token"]
        yield connection


def test_new_analysis_request_cannot_bypass_unknown_cost_retry_lock(client):
    repo = client.app.state.repo
    episode_id, _, job_id = seed(repo, state="failed")
    call_record(repo, job_id, call_state="interrupted", bill_state="unknown")
    retry = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert retry.status_code == 409
    response = client.post(f"/api/v1/episodes/{episode_id}/analysis")
    assert response.status_code == 409, "new-task endpoint must not evade unresolved same-step costs"


def test_retry_keeps_settled_without_actual_cost_frozen(client):
    repo = client.app.state.repo
    _, _, job_id = seed(repo, state="failed")
    call_record(repo, job_id, call_state="failed", bill_state="settled", cost=None)
    response = client.post(f"/api/v1/jobs/{job_id}/retry")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COST_UNKNOWN"


@pytest.mark.parametrize("version_state", ["draft", "ready", "confirmed"])
def test_completed_narration_for_same_immutable_version_is_not_requeued(client, version_state):
    repo = client.app.state.repo
    episode_id, asset_id, job_id = seed(repo, kind="narration", state="succeeded")
    voice = repo.path("outputs/already-generated.wav")
    with wave.open(str(voice), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\0\0" * 4800)
    with repo.transaction() as db:
        version = Version(owner_id="alice", episode_id=episode_id, title="已有旁白", state=version_state,
                          narration_text="已确认旁白", narration_approved=True, narration_path="outputs/already-generated.wav",
                          segments=[{"asset_id": asset_id, "start": 0, "end": 1, "caption": ""}], job_id=job_id)
        db.add(version)
        db.flush()
        version_id = version.id
        db.get(Job, job_id).version_id = version_id
        if version_state != "draft":
            render_job = Job(owner_id="alice", episode_id=episode_id, version_id=version_id,
                             kind="render", state="succeeded", idempotency_key="completed-render")
            db.add(render_job)
            db.flush()
            version.job_id = render_job.id
    response = client.post(f"/api/v1/versions/{version_id}/narration", json={"confirm_text": True})
    assert response.status_code == 200
    assert response.json()["id"] == job_id
    with repo.factory() as db:
        assert db.scalar(select(func.count()).select_from(Job).where(Job.kind == "narration")) == 1
