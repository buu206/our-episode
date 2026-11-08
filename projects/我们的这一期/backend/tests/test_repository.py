"""Budget and crash-recovery checks against isolated on-disk SQLite databases."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from sqlalchemy import func, select

from app.models import Audit, Budget, Episode, Job, Version
from app.repository import DomainError, Repository


@pytest.fixture
def repo(tmp_path):
    instance = Repository(tmp_path / "isolated-data")
    yield instance
    instance.engine.dispose()


def episode(repo, owner="owner-a"):
    with repo.transaction() as db:
        row = Episode(owner_id=owner, title="预算验证")
        db.add(row)
        db.flush()
        return row.id


def reserve(repo, episode_id, amount, owner="owner-a"):
    with repo.transaction() as db:
        return repo.reserve(db, owner, episode_id, amount)


def totals(repo, episode_id):
    with repo.factory() as db:
        return repo.budget(db, episode_id)


@pytest.mark.parametrize("amount", [0, -1, True, False, float("nan"), float("inf"),
                                    float("-inf"), "1", None])
def test_reservation_rejects_invalid_amount_without_a_row(repo, amount):
    episode_id = episode(repo)
    with pytest.raises(DomainError) as error:
        reserve(repo, episode_id, amount)
    assert error.value.code == "INVALID_AMOUNT"
    assert totals(repo, episode_id)["total_reserved"] == 0


@pytest.mark.parametrize("amount", [1e308, 10 ** 400], ids=["large-float", "large-int"])
def test_huge_finite_amount_is_rejected_without_numeric_overflow(repo, amount):
    with pytest.raises(DomainError) as error:
        reserve(repo, episode(repo), amount)
    assert error.value.code in ("INVALID_AMOUNT", "BUDGET_EXCEEDED")


def test_exact_episode_limit_and_one_micro_yuan_over(repo):
    episode_id = episode(repo)
    reserve(repo, episode_id, 1.82)
    reserve(repo, episode_id, 1.18)
    assert totals(repo, episode_id)["reserved"] == 3
    with pytest.raises(DomainError) as error:
        reserve(repo, episode_id, 0.000001)
    assert error.value.code == "BUDGET_EXCEEDED"
    assert totals(repo, episode_id)["reserved"] == 3


def test_positive_sub_micro_amount_rounds_up_and_cannot_bypass_limit(repo):
    episode_id = episode(repo)
    reservation = reserve(repo, episode_id, 1e-12)
    with repo.factory() as db:
        assert db.get(Budget, reservation).amount == 1
    reserve(repo, episode_id, 2.999999)
    with pytest.raises(DomainError):
        reserve(repo, episode_id, 1e-12)


def test_batch_limit_includes_other_episodes_and_owners(repo):
    for index in range(10):
        owner = f"owner-{index}"
        reserve(repo, episode(repo, owner), 3, owner)
    fresh = episode(repo, "another-owner")
    assert totals(repo, fresh)["reserved"] == 0
    assert totals(repo, fresh)["total_reserved"] == 30
    with pytest.raises(DomainError) as error:
        reserve(repo, fresh, 0.000001, "another-owner")
    assert error.value.code == "BUDGET_EXCEEDED"


@pytest.mark.parametrize("state", ["reserved", "unknown", "settled"])
def test_missing_actual_cost_keeps_reservation_occupied(repo, state):
    episode_id = episode(repo)
    reservation = reserve(repo, episode_id, 1.82)
    with repo.transaction() as db:
        row = db.get(Budget, reservation)
        row.state, row.cost = state, None
    assert totals(repo, episode_id)["reserved"] == 1.82
    assert totals(repo, episode_id)["spent"] == 0
    with pytest.raises(DomainError):
        reserve(repo, episode_id, 1.180001)


def test_confirmed_cost_releases_only_unused_reservation(repo):
    episode_id = episode(repo)
    reservation = reserve(repo, episode_id, 1.82)
    with repo.transaction() as db:
        row = db.get(Budget, reservation)
        row.state, row.cost = "settled", 300_000
    budget = totals(repo, episode_id)
    assert budget["spent"] == 0.3 and budget["reserved"] == 0
    reserve(repo, episode_id, 2.7)
    with pytest.raises(DomainError):
        reserve(repo, episode_id, 0.000001)


def test_explicit_known_zero_cost_can_release_reserved_amount(repo):
    episode_id = episode(repo)
    reservation = reserve(repo, episode_id, 1.82)
    with repo.transaction() as db:
        row = db.get(Budget, reservation)
        row.state, row.cost = "settled", 0
    reserve(repo, episode_id, 3)
    assert totals(repo, episode_id)["reserved"] == 3


def test_transaction_rollback_does_not_leave_phantom_reservation(repo):
    episode_id = episode(repo)
    with pytest.raises(RuntimeError):
        with repo.transaction() as db:
            repo.reserve(db, "owner-a", episode_id, 1)
            raise RuntimeError("abort this transaction")
    assert totals(repo, episode_id)["total_reserved"] == 0


def test_concurrent_reservations_cannot_both_pass_same_remaining_budget(repo):
    episode_id = episode(repo)
    gate = threading.Barrier(2)

    def attempt():
        gate.wait(timeout=5)
        try:
            reserve(repo, episode_id, 2)
            return "reserved"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        results = sorted(future.result(timeout=10) for future in futures)
    assert results == ["BUDGET_EXCEEDED", "reserved"]
    assert totals(repo, episode_id)["reserved"] == 2


def test_reopen_restores_budget_and_marks_only_running_jobs_interrupted(repo):
    episode_id = episode(repo)
    running_reservation = reserve(repo, episode_id, 1.82)
    queued_reservation = reserve(repo, episode_id, 1)
    with repo.transaction() as db:
        rendering = Version(owner_id="owner-a", episode_id=episode_id, title="处理中版本",
                            state="rendering", segments=[])
        db.add(rendering)
        db.flush()
        running = Job(owner_id="owner-a", episode_id=episode_id, version_id=rendering.id,
                      kind="direction", state="running", idempotency_key="running-case",
                      payload={"reservation": running_reservation})
        queued = Job(owner_id="owner-a", episode_id=episode_id, kind="direction", state="queued",
                     idempotency_key="queued-case", payload={"reservation": queued_reservation})
        succeeded = Job(owner_id="owner-a", episode_id=episode_id, kind="render", state="succeeded",
                        idempotency_key="finished-case", result={"version_id": "finished-version"})
        db.add_all([running, queued, succeeded])
        db.flush()
        ids = running.id, queued.id, succeeded.id, rendering.id
    data_dir = repo.data_dir
    repo.engine.dispose()
    reopened = Repository(data_dir)
    try:
        reopened.recover()
        reopened.recover()  # Repeated process recovery must not duplicate interruption events.
        with reopened.factory() as db:
            interrupted = db.get(Job, ids[0])
            assert interrupted.state == "failed"
            assert interrupted.error["code"] == "INTERRUPTED"
            assert interrupted.payload["reservation"] == running_reservation
            assert db.get(Version, ids[3]).state == "failed"
            assert db.get(Version, ids[3]).error["code"] == "INTERRUPTED"
            assert db.get(Budget, running_reservation).state == "unknown"
            assert db.get(Budget, running_reservation).cost is None
            assert db.get(Job, ids[1]).state == "queued"
            assert db.get(Budget, queued_reservation).state == "reserved"
            assert db.get(Job, ids[2]).state == "succeeded"
            assert db.get(Job, ids[2]).result == {"version_id": "finished-version"}
            assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "job_interrupted")) == 1
        assert totals(reopened, episode_id)["reserved"] == 2.82
        with pytest.raises(DomainError):
            reserve(reopened, episode_id, 0.180001)
    finally:
        reopened.engine.dispose()


def test_recovery_preserves_already_confirmed_version_and_known_cost(repo):
    episode_id = episode(repo)
    reservation = reserve(repo, episode_id, 1.82)
    with repo.transaction() as db:
        bill = db.get(Budget, reservation)
        bill.state, bill.cost = "settled", 150_000
        version = Version(owner_id="owner-a", episode_id=episode_id, title="已确认",
                          state="confirmed", segments=[], confirmed_at="2026-11-06T00:00:00Z")
        db.add(version)
        db.flush()
        version_id = version.id
        db.add(Job(owner_id="owner-a", episode_id=episode_id, version_id=version.id,
                   kind="render", state="running", idempotency_key="late-persist",
                   payload={"reservation": reservation}))
    repo.recover()
    with repo.factory() as db:
        assert db.get(Version, version_id).state == "confirmed"
        assert db.get(Budget, reservation).state == "settled"
        assert db.get(Budget, reservation).cost == 150_000
    assert totals(repo, episode_id)["spent"] == 0.15
    assert totals(repo, episode_id)["reserved"] == 0
