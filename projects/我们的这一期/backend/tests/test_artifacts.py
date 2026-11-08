"""Isolated disk/crash tests; no cloud, user data, or real model calls."""

from pathlib import Path
from contextlib import contextmanager
import subprocess
import sys

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import Asset, Episode, Job, Version, uid
from app.repository import DomainError, Repository
from app.services import artifacts
from app.services.artifacts import artifact_workspace, record_artifact, recover_artifacts, release_artifact
from app.services.cleanup import cleanup_deleted_files
from app.worker import Worker


@pytest.fixture
def repo(tmp_path):
    value = Repository(tmp_path / "isolated-artifacts")
    yield value
    value.engine.dispose()


def seed(repo, *, owner="alice", state="failed", kind="analysis", deleted=False):
    relative = "assets/" + uid() + ".mp4"
    repo.path(relative).write_bytes(b"application-copy")
    with repo.transaction() as db:
        episode = Episode(owner_id=owner, title="临时文件恢复")
        db.add(episode)
        db.flush()
        asset = Asset(owner_id=owner, episode_id=episode.id, filename="source.mp4", path=relative,
                      size=16, duration=2, width=1280, height=720, has_audio=True, deleted=deleted)
        db.add(asset)
        db.flush()
        job = Job(owner_id=owner, episode_id=episode.id, kind=kind, state=state,
                  idempotency_key=uid(), payload={"asset_ids": [asset.id]})
        db.add(job)
        db.flush()
        return episode.id, asset.id, job.id


def registry(repo, job_id):
    with repo.factory() as db:
        return db.get(Job, job_id).payload.get("artifacts", [])


def registered_file(repo, job_id, suffix="wav"):
    relative = "outputs/" + uid() + "." + suffix
    record_artifact(repo, job_id, relative)
    repo.path(relative).write_bytes(b"temporary-test-output")
    return relative


@pytest.mark.parametrize("relative,kind", [
    ("assets/" + "a" * 32 + ".mp4", "file"),
    ("outputs/../source.mp4", "file"),
    ("/tmp/source.wav", "file"),
    ("outputs/" + "a" * 32 + ".exe", "file"),
    ("incoming/work-other-product", "directory"),
    ("incoming/work-" + "a" * 32 + "/child", "directory"),
    ("outputs/" + "a" * 32 + ".wav", "recursive"),
])
def test_registration_refuses_noncontrolled_paths(repo, relative, kind):
    _, _, job_id = seed(repo)
    with pytest.raises(DomainError, match="受控范围"):
        record_artifact(repo, job_id, relative, kind)
    assert registry(repo, job_id) == []


def test_registry_precedes_write_and_cannot_claim_existing_or_other_jobs_file(repo):
    _, _, first = seed(repo)
    _, _, second = seed(repo, owner="bob")
    relative = "outputs/" + uid() + ".wav"
    record_artifact(repo, first, relative)
    assert not repo.path(relative).exists()
    assert registry(repo, first) == [{"path": relative, "kind": "file"}]
    record_artifact(repo, first, relative)  # Idempotent registration before write.
    with pytest.raises(DomainError) as error:
        record_artifact(repo, second, relative)
    assert error.value.code == "ARTIFACT_OWNED"
    existing = "outputs/" + uid() + ".mp4"
    repo.path(existing).write_bytes(b"must-survive")
    with pytest.raises(DomainError) as error:
        record_artifact(repo, second, existing)
    assert error.value.code == "ARTIFACT_EXISTS"
    assert repo.path(existing).read_bytes() == b"must-survive"


@pytest.mark.parametrize("fail", [False, True])
def test_workspace_cleans_whole_nested_tree_on_exit(repo, fail):
    _, _, job_id = seed(repo)
    def operation():
        with artifact_workspace(repo, job_id) as directory:
            assert registry(repo, job_id) == [{"path": str(directory.relative_to(repo.data_dir)), "kind": "directory"}]
            (directory / ".renderer-temp").mkdir()
            (directory / ".renderer-temp" / "frames.raw").write_bytes(b"private")
            if fail:
                raise RuntimeError("offline failure")
        return directory
    if fail:
        with pytest.raises(RuntimeError, match="offline failure"):
            operation()
    else:
        assert not operation().exists()
    assert registry(repo, job_id) == []
    assert list(repo.path("incoming").iterdir()) == []


def test_failed_workspace_cleanup_is_durable_and_retried(repo, monkeypatch):
    _, _, job_id = seed(repo)
    with monkeypatch.context() as patch:
        patch.setattr(artifacts.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(PermissionError("busy")))
        with artifact_workspace(repo, job_id) as directory:
            (directory / "private.wav").write_bytes(b"private")
    assert directory.exists() and registry(repo, job_id)
    result = recover_artifacts(repo)
    assert result.attempted == 1 and result.pending == 0
    assert not directory.exists() and registry(repo, job_id) == []


def test_workspace_registry_write_failure_does_not_invalidate_committed_output(repo, monkeypatch):
    _, _, job_id = seed(repo)
    transaction = repo.transaction
    calls = 0
    @contextmanager
    def unavailable_during_cleanup():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SQLAlchemyError("offline database write failure")
        with transaction() as db:
            yield db
    with monkeypatch.context() as patch:
        patch.setattr(repo, "transaction", unavailable_during_cleanup)
        with artifact_workspace(repo, job_id) as directory:
            (directory / "work.wav").write_bytes(b"private")
    assert not directory.exists() and registry(repo, job_id)
    assert recover_artifacts(repo).pending == 0
    assert registry(repo, job_id) == []


def test_recovery_never_sweeps_unregistered_or_source_files(repo):
    _, asset_id, job_id = seed(repo)
    temporary = registered_file(repo, job_id)
    unregistered = repo.path("outputs/" + uid() + ".wav")
    unregistered.write_bytes(b"not-owned-by-registry")
    outside = repo.data_dir.parent / "user-original.mp4"
    outside.write_bytes(b"original")
    with repo.factory() as db:
        source = repo.path(db.get(Asset, asset_id).path)
    result = recover_artifacts(repo)
    assert result.attempted == 1 and result.pending == 0
    assert not repo.path(temporary).exists() and registry(repo, job_id) == []
    assert source.exists() and unregistered.exists() and outside.read_bytes() == b"original"


@pytest.mark.parametrize("reference", ["output_path", "narration_path", "checkpoint-relative", "checkpoint-absolute"])
def test_recovery_preserves_published_files_and_paid_tts_checkpoints(repo, reference):
    episode_id, _, job_id = seed(repo)
    relative = registered_file(repo, job_id)
    with repo.transaction() as db:
        if reference.startswith("checkpoint"):
            value = str(repo.path(relative)) if reference.endswith("absolute") else relative
            job = db.get(Job, job_id)
            job.payload = {**job.payload, "steps": {"tts": {"path": value}}}
        else:
            db.add(Version(owner_id="alice", episode_id=episode_id, title="已保存", segments=[], **{reference: relative}))
    result = recover_artifacts(repo)
    assert result.retained == 1 and result.attempted == 0
    assert repo.path(relative).exists() and registry(repo, job_id)


def test_running_task_is_protected_even_from_corrupt_duplicate_registration(repo):
    _, _, active = seed(repo, owner="bob", state="running")
    _, _, abandoned = seed(repo)
    relative = registered_file(repo, active)
    with repo.transaction() as db:
        db.get(Job, abandoned).payload = {"artifacts": [{"path": relative, "kind": "file"}]}
    result = recover_artifacts(repo)
    assert result.retained == 1 and repo.path(relative).exists()
    assert registry(repo, active) and registry(repo, abandoned)


def test_recovery_unlinks_symlinks_without_touching_external_targets(repo):
    _, _, job_id = seed(repo)
    outside = repo.data_dir.parent / "external-original.wav"
    outside.write_bytes(b"external")
    relative = "outputs/" + uid() + ".wav"
    record_artifact(repo, job_id, relative)
    lexical_path = repo.data_dir / relative
    lexical_path.symlink_to(outside)
    work = "incoming/work-" + uid()
    record_artifact(repo, job_id, work, "directory")
    directory = repo.data_dir / work
    directory.mkdir()
    (directory / "external-link").symlink_to(outside)
    result = recover_artifacts(repo)
    assert result.attempted == 2 and result.pending == 0
    assert not lexical_path.is_symlink() and not directory.exists()
    assert outside.read_bytes() == b"external"


def test_release_commits_atomically_with_publication(repo):
    episode_id, _, job_id = seed(repo)
    relative = registered_file(repo, job_id, "mp4")
    with pytest.raises(RuntimeError):
        with repo.transaction() as db:
            release_artifact(db, db.get(Job, job_id), relative)
            db.add(Version(owner_id="alice", episode_id=episode_id, title="未提交", segments=[], output_path=relative))
            raise RuntimeError("simulated crash before commit")
    assert registry(repo, job_id)
    with repo.transaction() as db:
        release_artifact(db, db.get(Job, job_id), relative)
        db.add(Version(owner_id="alice", episode_id=episode_id, title="已提交", segments=[], output_path=relative))
    assert registry(repo, job_id) == []
    recover_artifacts(repo)
    assert repo.path(relative).exists()


def test_abrupt_process_exit_leaves_manifest_for_restart_cleanup(repo):
    _, _, job_id = seed(repo, state="running")
    script = """
from pathlib import Path
import os, sys
from app.repository import Repository
from app.services.artifacts import artifact_workspace, record_artifact
repo = Repository(Path(sys.argv[1]))
record_artifact(repo, sys.argv[2], 'outputs/' + 'a' * 32 + '.mp4')
repo.path('outputs/' + 'a' * 32 + '.mp4').write_bytes(b'partial')
with artifact_workspace(repo, sys.argv[2]) as path:
    (path / 'private-audio.wav').write_bytes(b'partial')
    os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", script, str(repo.data_dir), job_id],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)
    assert result.returncode == 17
    assert len(registry(repo, job_id)) == 2
    repo.recover()
    result = recover_artifacts(repo)
    assert result.attempted == 2 and result.pending == 0
    assert registry(repo, job_id) == []
    assert list(repo.path("incoming").iterdir()) == []
    assert list(repo.path("outputs").iterdir()) == []


def test_deleted_asset_cleans_owned_artifacts_and_paid_checkpoint_only(repo):
    episode_id, asset_id, job_id = seed(repo, deleted=True)
    _, _, other_job = seed(repo, owner="bob", state="running")
    other_file = registered_file(repo, other_job)
    temporary = registered_file(repo, job_id)
    paid = "outputs/" + uid() + ".wav"
    repo.path(paid).write_bytes(b"paid-checkpoint")
    work = "incoming/work-" + uid()
    record_artifact(repo, job_id, work, "directory")
    repo.path(work).mkdir()
    (repo.path(work) / "private.wav").write_bytes(b"private")
    with repo.transaction() as db:
        job = db.get(Job, job_id)
        job.payload = {**job.payload, "steps": {"tts": {"path": str(repo.path(paid))}}}
        source = repo.path(db.get(Asset, asset_id).path)
    result = cleanup_deleted_files(repo, asset_id=asset_id, owner_id="alice")
    assert result.pending == 0 and result.attempted == 4
    assert not source.exists() and not repo.path(temporary).exists() and not repo.path(paid).exists()
    assert not repo.path(work).exists() and registry(repo, job_id) == []
    assert repo.path(other_file).exists() and registry(repo, other_job)


def test_deleted_asset_finds_render_job_through_version_when_payload_has_no_assets(repo):
    episode_id, asset_id, job_id = seed(repo, kind="render", deleted=True)
    temporary = registered_file(repo, job_id, "mp4")
    with repo.transaction() as db:
        version = Version(owner_id="alice", episode_id=episode_id, title="受限", state="restricted",
                          segments=[{"asset_id": asset_id, "start": 0, "end": 1}])
        db.add(version)
        db.flush()
        job = db.get(Job, job_id)
        job.version_id = version.id
        job.payload = {"artifacts": job.payload["artifacts"]}
    result = cleanup_deleted_files(repo, asset_id=asset_id, owner_id="alice")
    assert result.pending == 0 and not repo.path(temporary).exists()
    assert registry(repo, job_id) == []


def test_deleted_asset_cleanup_retries_failed_unlink_without_losing_manifest(repo, monkeypatch):
    _, asset_id, job_id = seed(repo, deleted=True)
    relative = registered_file(repo, job_id)
    target = repo.path(relative)
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == target:
            raise PermissionError("busy")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        result = cleanup_deleted_files(repo, asset_id=asset_id, owner_id="alice")
    assert result.pending == 1 and target.exists() and registry(repo, job_id)
    assert cleanup_deleted_files(repo, asset_id=asset_id, owner_id="alice").pending == 0
    assert not target.exists() and registry(repo, job_id) == []


def test_revoked_job_cannot_clean_an_unrelated_active_tasks_duplicate_registration(repo):
    _, asset_id, deleted_job = seed(repo, deleted=True)
    _, _, active = seed(repo, owner="bob", state="running")
    relative = registered_file(repo, active)
    with repo.transaction() as db:
        job = db.get(Job, deleted_job)
        job.payload = {**job.payload, "artifacts": [{"path": relative, "kind": "file"}]}
    result = cleanup_deleted_files(repo, asset_id=asset_id, owner_id="alice")
    assert result.pending == 1 and repo.path(relative).exists()
    assert registry(repo, active) and registry(repo, deleted_job)


@pytest.mark.parametrize("delete_during_render", [False, True])
def test_worker_registers_scratch_and_output_then_publishes_or_revokes(repo, monkeypatch, delete_during_render):
    from app import worker
    episode_id, asset_id, job_id = seed(repo, kind="render", state="queued")
    with repo.transaction() as db:
        version = Version(owner_id="alice", episode_id=episode_id, title="离线渲染测试", state="rendering",
                          segments=[{"asset_id": asset_id, "start": 0, "end": 1, "caption": ""}])
        db.add(version)
        db.flush()
        version_id = version.id
        db.get(Job, job_id).version_id = version_id
    def offline_render(segments, output, *, title):
        manifest = registry(repo, job_id)
        assert any(item["kind"] == "directory" and repo.path(item["path"]) == output.parent for item in manifest)
        assert any(item["kind"] == "file" and item["path"].endswith(".mp4") for item in manifest)
        (output.parent / ".renderer-internal").mkdir()
        (output.parent / ".renderer-internal" / "partial").write_bytes(b"private")
        output.write_bytes(b"offline-render-result")
        if delete_during_render:
            with repo.transaction() as db:
                db.get(Asset, asset_id).deleted = True
                db.get(Version, version_id).state = "restricted"
        return {"duration": 1, "width": 1280, "height": 720}
    monkeypatch.setattr(worker, "render", offline_render)
    Worker(repo).process_one()
    with repo.factory() as db:
        job, version = db.get(Job, job_id), db.get(Version, version_id)
        assert job.state == ("failed" if delete_during_render else "succeeded")
        assert version.state == ("restricted" if delete_during_render else "ready")
        if delete_during_render:
            assert version.output_path is None
            assert list(repo.path("outputs").iterdir()) == []
        else:
            assert repo.path(version.output_path).read_bytes() == b"offline-render-result"
    assert registry(repo, job_id) == []
    assert list(repo.path("incoming").iterdir()) == []
