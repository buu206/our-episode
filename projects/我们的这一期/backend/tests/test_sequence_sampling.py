"""Short-window detail sampling stays bounded and legacy jobs keep their recipe."""
import pytest
from app.models import Version, Job
from app.repository import DomainError
from app.services import sequence_review as sr
from app.services.pipeline import ModelServices
from test_sequence_review import rendered, client, setup, movie, confirmed, offline, KEYS


@pytest.mark.parametrize('start,end,count', [(0., 6., 24), (0., 8., 9), (7.9, 8., 2)])
def test_short_detail_and_long_overview_keep_frame_and_budget_caps(rendered, start, end, count):
    _, repo, _, vid, _ = rendered
    with repo.transaction() as db:
        job = sr.enqueue(repo, db, db.get(Version, vid), ModelServices(KEYS), {'start': start, 'end': end})
        jid = job.id
    frames = sr.prepare(repo, jid)
    assert len(frames) == count <= sr.MAX_FRAMES
    assert all(start < f['time'] < end for f in frames)
    assert len({f['time'] for f in frames}) == count
    assert sr.RESERVATION == .10


def test_legacy_sampling_is_not_silently_changed_or_reused_as_dense(rendered):
    _, repo, grant, vid, _ = rendered
    window = {'start': 1., 'end': 3.}
    with repo.transaction() as db:
        version = db.get(Version, vid)
        bound = sr.snapshot(repo, db, version, window)
        old = Job(owner_id=grant['owner_id'], episode_id=grant['episode_id'], version_id=vid,
                  kind='sequence_review', idempotency_key=sr.REVISION + ':' + sr.digest(bound),
                  payload={'window': window, 'snapshot': bound})
        db.add(old)
        db.flush()
        old_id = old.id
        new = sr.enqueue(repo, db, version, ModelServices(KEYS), window)
        assert new.id != old.id
        new_id = new.id
    assert len(sr.prepare(repo, old_id)) == 3
    assert len(sr.prepare(repo, new_id)) == 9
    with repo.transaction() as db:
        again = sr.enqueue(repo, db, db.get(Version, vid), ModelServices(KEYS), window)
        assert again.id == new_id


def test_unknown_sampling_profile_is_rejected(rendered):
    _, repo, _, vid, _ = rendered
    with repo.factory() as db:
        with pytest.raises(DomainError, match='采样规则'):
            sr.snapshot(repo, db, db.get(Version, vid), {'start': 1., 'end': 3.}, sampling='unsupported')


def test_less_than_two_real_frames_stops_before_any_cloud_call(rendered):
    _, repo, _, vid, _ = rendered
    with repo.transaction() as db:
        job = sr.enqueue(repo, db, db.get(Version, vid), ModelServices(KEYS), {'start': 7.99, 'end': 8.})
        jid = job.id
    with pytest.raises(DomainError) as error:
        sr.prepare(repo, jid)
    assert error.value.code == 'REVIEW_WINDOW_TOO_SHORT'
    from sqlalchemy import select
    from app.models import ModelCall
    with repo.factory() as db: assert not list(db.scalars(select(ModelCall)))
