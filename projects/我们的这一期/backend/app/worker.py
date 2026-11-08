import fcntl
import os
import threading
from sqlalchemy import select
from .models import Asset, Episode, Job, Version, uid
from .repository import DomainError
from .services.media import MediaError, render
from .services.artifacts import artifact_workspace, record_artifact, recover_artifacts, release_artifact
from .services.collaboration import guard_asset, run_collaboration_job, cleanup_collaboration


def source_segments(repo, db, version):
    if version.evidence_revision is not None:
        episode = repo.owned(db, Episode, version.episode_id, version.owner_id)
        if episode.evidence_revision != version.evidence_revision or episode.evidence_confirmed_revision != version.evidence_revision:
            raise DomainError('EVIDENCE_CHANGED', '版本依据已更新，请重新核对并生成。', 409)
    segments = []
    for item in version.segments:
        asset = repo.owned(db, Asset, item['asset_id'], version.owner_id)
        if asset.deleted or asset.episode_id != version.episode_id:
            raise DomainError('SOURCE_REMOVED', '引用素材已删除，该版本不能继续使用。', 409)
        guard_asset(db, asset, item)
        path = repo.path(asset.path)
        if not path.is_file():
            raise DomainError('SOURCE_MISSING', '应用内素材副本丢失，请重新上传。', 409)
        segments.append({**item, 'path': path})
    return segments


class Worker:
    """One process, one serial worker; durable jobs, explicit interrupted retries."""
    def __init__(self, repo, pipeline=None):
        self.repo = repo
        self.pipeline = pipeline
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = None

    def start(self):
        self.lock = (self.repo.data_dir / '.worker.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise RuntimeError('此数据目录已由另一个服务处理，请使用单进程启动。') from None
        self.repo.recover()
        recover_artifacts(self.repo)
        cleanup_collaboration(self.repo)
        self.thread = threading.Thread(target=self.run, daemon=True, name='local-render')
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()  # FFmpeg has bounded subprocess timeouts.
        if self.lock:
            self.lock.close()

    def run(self):
        while not self.stop_event.is_set():
            self.process_one()
            self.stop_event.wait(.25)

    def process_one(self):
        job_id = None
        output = None
        try:
            with self.repo.transaction() as db:
                job = db.scalars(select(Job).where(Job.state == 'queued').order_by(Job.created_at)).first()
                if not job:
                    return
                job_id = job.id
                kind = job.kind
                if kind == 'render':
                    version = self.repo.owned(db, Version, job.version_id, job.owner_id)
                    if version.state == 'restricted':
                        raise DomainError('SOURCE_REMOVED', '素材已删除，已停止生成。', 409)
                    segments = source_segments(self.repo, db, version)
                    title = version.title if version.packaging != 'original' else ''
                    if version.packaging != 'standard':
                        segments = [{**s, 'caption': ''} for s in segments]
                    narration = self.repo.path(version.narration_path) if version.narration_path else None
                    if version.narration_text and (not version.narration_approved or not narration or not narration.is_file()):
                        raise DomainError('NARRATION_REQUIRED', '旁白未确认或文件缺失，请先完成旁白。', 409)
                    label = '正在本机渲染画面、花字与原声'
                elif kind in ('analysis', 'directions', 'narration') and self.pipeline:
                    self.pipeline._validate_job(db, job)
                    label = '正在检查来源与本期预算'
                elif kind in ('context_preview', 'submission_preview', 'accept_submission'):
                    label = '正在本机处理授权片段'
                else:
                    raise DomainError('UNSUPPORTED_JOB', '此任务类型尚未开放。', 409)
                job.state, job.stage = 'running', label
                self.repo.audit(db, job.owner_id, kind + '_started', job.id)
            if kind != 'render':
                if kind in ('context_preview', 'submission_preview', 'accept_submission'):
                    run_collaboration_job(self.repo, job_id)
                else:
                    self.pipeline.run(job_id, kind)
                return
            relative = 'outputs/' + uid() + '.mp4'
            output = self.repo.path(relative)
            record_artifact(self.repo, job_id, relative)
            # Both renderers create their own scratch directories beside their
            # target. Keep that entire tree within a durably registered workspace.
            with artifact_workspace(self.repo, job_id) as directory:
                complete = directory / 'complete.mp4'
                if narration:
                    from .services.packaging import add_narrated_intro
                    cut = directory / 'cut.mp4'
                    render(segments, cut, title=title)
                    result = add_narrated_intro(cut, narration, complete, title=title)
                else:
                    result = render(segments, complete, title=title)
                with self.repo.transaction() as db:
                    job = db.get(Job, job_id)
                    version = db.get(Version, job.version_id)
                    source_segments(self.repo, db, version)  # Deletion may occur during encoding.
                    if version.state == 'restricted' or job.state != 'running':
                        raise DomainError('SOURCE_REMOVED', '任务或素材已失效，成片不可使用。', 409)
                    os.link(complete, output)
                    version.state, version.output_path, version.error = 'ready', relative, None
                    release_artifact(db, job, relative)
                    job.state, job.stage, job.result = 'succeeded', '已生成，请预览并确认', result
                    self.repo.audit(db, job.owner_id, 'render_succeeded', job.id)
        except Exception as exc:
            if output and (output.exists() or output.is_symlink()):
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass  # No business reference is published; leave for explicit cleanup.
            if not job_id:
                return
            error = ({'code': exc.code, 'message': exc.message} if isinstance(exc, (DomainError, MediaError))
                     else {'code': 'PROCESSING_FAILED', 'message': '处理失败；已保留输入和完成的检查点。'})
            with self.repo.transaction() as db:
                job = db.get(Job, job_id)
                job.state, job.stage, job.error = 'failed', '处理失败，可查看原因后重试', error
                version = db.get(Version, job.version_id) if job.version_id else None
                if version and version.state != 'restricted':
                    version.state, version.error = 'failed', error
                if output and not output.exists() and not output.is_symlink():
                    release_artifact(db, job, str(output.relative_to(self.repo.data_dir)))
                self.repo.audit(db, job.owner_id, job.kind + '_failed', job.id, code=error['code'])
