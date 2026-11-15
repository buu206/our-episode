#!/usr/bin/env python3
"""Real production analysis of one immutable, fictional, locally generated clip.

Operator-only scope; never imported by the server. Normal privacy flags remain
off. Each step stops at the next human decision and never confirms for the user.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PRODUCT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PRODUCT / 'backend'))
from sqlalchemy import select
from app.models import Asset, Budget, Episode, Job, ModelCall
from app.repository import DomainError, Repository
from app.retention_models import Retention
from app.services import media
from app.services.artifacts import artifact_workspace
from app.services.deepseek import DeepSeekVision, DeepSeekDirector, MODEL as VISION_MODEL
from app.services.director import DirectorError
from app.services.minimax_asr import MiniMaxASR, MODEL as ASR_MODEL
from app.services.pipeline import ModelServices, Pipeline
from app.services.speech import _chunk_data, SpeechError
from cloud_preflight import read_configuration
from synthetic_deepseek import fixtures, check_key
from synthetic_minimax_asr import SOURCE, SOURCE_SHA

SCOPE = 'synthetic-chain-v1-approved-20261112'
OWNER = 'local-owner'  # Existing normal login; this clip is visible in its studio.
EPISODE_ID = hashlib.sha256(SCOPE.encode()).hexdigest()[:32]
ASSET_ID = hashlib.sha256((SCOPE + ':asset').encode()).hexdigest()[:32]
FIXTURE_SHA = 'ec79087a060089a8e7811cf612955a336c4d8bd564fe9b637757a8b77d2270d6'
RELATIVE = f'assets/{ASSET_ID}.mov'
BACKGROUND = ('固定非个人技术样片：白底左红圆、右蓝方的静态几何图案；声音为已确认的合成虚构旁白。'
              '声音中的做饭与外卖只是虚构文案，画面未出现这些事件；不推断真人、身份或画面动作。')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def audio_signature(chunks):
    return [(c.asset_id, c.offset, c.duration, hashlib.sha256(data).hexdigest()) for c, data in _chunk_data(chunks)]


def build_fixture(voice, directory):
    (directory / 'voice.wav').write_bytes(voice)
    (directory / 'frame.jpg').write_bytes(fixtures()[0].frames[0].jpeg)
    movie = directory / 'fixture.mov'
    subprocess.run([media._binary('ffmpeg'), '-nostdin', '-v', 'error', '-n',
        '-loop', '1', '-framerate', '25', '-i', str(directory/'frame.jpg'), '-i', str(directory/'voice.wav'),
        '-t', '8', '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-threads', '2',
        '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-map_metadata', '-1',
        '-movflags', '+faststart', str(movie)], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    return movie


def verify_fixture(repo):
    path = repo.data_dir / RELATIVE
    if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
            or path.stat().st_size > 2_000_000 or hashlib.sha256(path.read_bytes()).hexdigest() != FIXTURE_SHA):
        raise DomainError('FIXTURE_CHANGED', '技术样片指纹不符或文件缺失，不发送其他素材。')
    with repo.factory() as db:
        episode = repo.owned(db, Episode, EPISODE_ID, OWNER)
        asset = repo.owned(db, Asset, ASSET_ID, OWNER)
        all_assets = list(db.scalars(select(Asset).where(Asset.episode_id == EPISODE_ID, Asset.deleted.is_(False))))
        if (episode.background != BACKGROUND or len(all_assets) != 1 or asset.deleted
                or asset.episode_id != EPISODE_ID or asset.path != RELATIVE
                or asset.duration != 8.0 or asset.width != 512 or asset.height != 512 or not asset.has_audio):
            raise DomainError('FIXTURE_CHANGED', '技术节目来源或背景已改变，暂停固定样例调用。')
    return path


def prepare(repo):
    with repo.transaction() as db:
        ep = db.get(Episode, EPISODE_ID)
        if ep is None:
            ep = Episode(id=EPISODE_ID, owner_id=OWNER, title='在场 · 固定样片真实AI联调', background=BACKGROUND)
            db.add(ep); db.flush()
            db.add(Retention(episode_id=ep.id, owner_id=OWNER, expires_at=time.time() + 30 * 86400))
        repo.owned(db, Episode, EPISODE_ID, OWNER)
        if db.get(Asset, ASSET_ID):
            exists = True
        else:
            exists = False
        key = SCOPE + ':prepare'
        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == key)).first()
        if job is None:
            job = Job(owner_id=OWNER, episode_id=EPISODE_ID, kind='synthetic_fixture', state='running',
                      idempotency_key=key, payload={'asset_ids': [], 'revision': 0})
            db.add(job); db.flush()
        jid = job.id
    if exists:
        return verify_fixture(repo)
    try:
        with artifact_workspace(repo, jid) as directory:
            original = repo.data_dir / SOURCE
            if original.is_symlink() or original.stat().st_size > 1_500_000:
                raise DomainError('SOURCE_CHANGED', '批准的虚构旁白文件无效。')
            voice = original.read_bytes()
            if hashlib.sha256(voice).hexdigest() != SOURCE_SHA:
                raise DomainError('SOURCE_CHANGED', '批准的虚构旁白指纹不符。')
            movie = build_fixture(voice, directory)
            if hashlib.sha256(movie.read_bytes()).hexdigest() != FIXTURE_SHA:
                raise DomainError('FIXTURE_CHANGED', '本机生成文件与固定指纹不同，未发送。')
            info = media.probe(movie)
            target = repo.data_dir / RELATIVE
            with repo.transaction() as db:
                repo.owned(db, Episode, EPISODE_ID, OWNER)
                if target.is_symlink():
                    raise DomainError('FIXTURE_CHANGED', '样片目标不是普通文件。')
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != FIXTURE_SHA:
                        raise DomainError('FIXTURE_CHANGED', '样片位置已有其他文件，不覆盖。')
                else:
                    os.link(movie, target)
                db.add(Asset(id=ASSET_ID, owner_id=OWNER, episode_id=EPISODE_ID,
                    filename='固定几何与虚构旁白.mov', path=RELATIVE, size=target.stat().st_size,
                    **{k: info[k] for k in ('duration', 'width', 'height', 'has_audio')}))
                job = db.get(Job, jid)
                job.state, job.stage = 'succeeded', '固定技术样片已准备，尚未调用模型'
                repo.audit(db, OWNER, 'synthetic_fixture_created', ASSET_ID, scope=SCOPE, sha256=FIXTURE_SHA)
        return verify_fixture(repo)
    except Exception:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.error = 'failed', {'code': 'PREPARATION_FAILED', 'message': '固定样片准备未完成。'}
        raise


class ScopedVision(DeepSeekVision):
    def __init__(self, key, expected_digest, authorized, **kwargs):
        self.expected_digest, self.authorized = expected_digest, authorized
        super().__init__({'DEEPSEEK_API_KEY': key}, **kwargs)

    def _guard(self):
        if self.authorized is not True:
            raise DirectorError('SYNTHETIC_NOT_AUTHORIZED', '仅允许已授权技术样片。')
        try:
            check_key(self._config['DEEPSEEK_API_KEY'])
        except DomainError as exc:
            raise DirectorError(exc.code, exc.message) from None

    async def analyze(self, prepared, *, reservation=None):
        if prepared.request_digest != self.expected_digest:
            raise DirectorError('SYNTHETIC_INPUT_REJECTED', '抽帧内容不属于固定技术样片。')
        return await super().analyze(prepared, reservation=reservation)


class ScopedASR(MiniMaxASR):
    def __init__(self, key, expected_signature, authorized, **kwargs):
        self.expected_signature, self.authorized = expected_signature, authorized
        super().__init__({'MINIMAX_API_KEY': key}, **kwargs)

    def _guard(self):
        key = self._config.get('MINIMAX_API_KEY')
        if self.authorized is not True:
            raise SpeechError('SYNTHETIC_NOT_AUTHORIZED', '仅允许已授权技术样片。')
        if not isinstance(key, str) or not key or not key.isascii() or any(c.isspace() for c in key):
            raise SpeechError('KEY_NOT_CONFIGURED', 'MiniMax密钥未配置。')

    async def transcribe(self, chunks, *, reservation=None):
        if audio_signature(chunks) != self.expected_signature:
            raise SpeechError('SYNTHETIC_INPUT_REJECTED', '提取音频不属于固定技术样片。')
        return await super().transcribe(chunks, reservation=reservation)


class ScopedDirector(DeepSeekDirector):
    def __init__(self, key, expected_input, authorized, **kwargs):
        self.expected_input, self.authorized = expected_input, authorized
        super().__init__({'DEEPSEEK_API_KEY': key}, **kwargs)

    def _guard(self):
        if self.authorized is not True:
            raise DirectorError('SYNTHETIC_NOT_AUTHORIZED', '仅允许已授权技术样片。')
        try:
            check_key(self._config['DEEPSEEK_API_KEY'])
        except DomainError as exc:
            raise DirectorError(exc.code, exc.message) from None

    async def propose(self, evidence, assets, background='', *, reservation=None):
        if digest([evidence, assets, background]) != self.expected_input:
            raise DirectorError('SYNTHETIC_INPUT_REJECTED', '编导输入不属于已确认的固定样片证据。')
        return await super().propose(evidence, assets, background, reservation=reservation)


class Services(ModelServices):
    def __init__(self, repo, keys, *, authorized=False, phase='analysis'):
        super().__init__({'VISION_PROVIDER': 'deepseek', 'ASR_PROVIDER': 'minimax'})
        self.repo, self.keys, self.authorized = repo, keys, authorized
        self.expected_vision, self.expected_audio = None, None
        self.phase = phase

    def ready(self):
        try:
            if self.authorized is not True:
                raise DomainError('SYNTHETIC_NOT_AUTHORIZED', '未授权固定技术样片。')
            verify_fixture(self.repo)
            if self.phase == 'directions':
                confirmed_input(self.repo)
            check_key(self.keys.get('DEEPSEEK_API_KEY'))
            ScopedASR(self.keys.get('MINIMAX_API_KEY'), [], True)._guard()
            return True, '仅固定样片；正常云准入仍关闭。'
        except (DomainError, SpeechError) as exc:
            return False, exc.message

    def profile(self):
        prompts = PRODUCT / 'backend/app/services/prompts'
        profile = {'scope': SCOPE, 'sample_type': 'synthetic-only', 'retention_verified': False,
                'fixture_sha256': FIXTURE_SHA, 'vision_model': VISION_MODEL, 'asr_model': ASR_MODEL,
                'vision_prompt_sha256': hashlib.sha256((prompts/'vision.txt').read_bytes()).hexdigest()}
        if self.phase == 'directions':
            profile.update(phase='directions', director_prompt_sha256=hashlib.sha256((prompts/'director.txt').read_bytes()).hexdigest(),
                           evidence_sha256=digest(confirmed_input(self.repo)))
        return profile

    def prepare_vision(self, assets, media_root):
        path = verify_fixture(self.repo)
        if assets != [{'asset_id': ASSET_ID, 'path': path}] or media_root != self.repo.data_dir/'assets':
            raise DomainError('SYNTHETIC_INPUT_REJECTED', '不读取固定样片以外的素材。')
        prepared = super().prepare_vision(assets, media_root)
        self.expected_vision = prepared.request_digest
        return prepared

    def vision_estimate(self, prepared):
        return DeepSeekVision.estimate(prepared)

    async def vision(self, prepared, reservation, permit):
        return await ScopedVision(self.keys['DEEPSEEK_API_KEY'], self.expected_vision,
            self.authorized, budget_check=permit).analyze(prepared, reservation=reservation)

    def prepare_audio(self, asset, path, directory):
        if asset.id != ASSET_ID or path != verify_fixture(self.repo):
            raise DomainError('SYNTHETIC_INPUT_REJECTED', '不读取固定样片以外的音频。')
        chunks = super().prepare_audio(asset, path, directory)
        self.expected_audio = audio_signature(chunks)
        return chunks

    async def asr(self, chunks, reservation, permit):
        return await ScopedASR(self.keys['MINIMAX_API_KEY'], self.expected_audio,
            self.authorized, budget_check=permit).transcribe(chunks, reservation=reservation)

    def director_estimate(self):
        return DeepSeekDirector.required_reservation_yuan

    async def directions(self, evidence, assets, background, reservation, permit):
        expected = digest(confirmed_input(self.repo))
        return await ScopedDirector(self.keys['DEEPSEEK_API_KEY'], expected,
            self.authorized, budget_check=permit).propose(evidence, assets, background, reservation=reservation)


def confirmed_input(repo):
    verify_fixture(repo)
    with repo.factory() as db:
        ep = repo.owned(db, Episode, EPISODE_ID, OWNER)
        if ep.analysis_state != 'confirmed' or ep.evidence_confirmed_revision != ep.evidence_revision:
            raise DomainError('REVIEW_REQUIRED', '请先由用户核对并确认固定样片证据，不自动代替确认。')
        analysis = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE+':analysis')).first()
        if (analysis is None or analysis.state != 'succeeded'
                or analysis.payload.get('synthetic_evidence_digest') != digest(ep.evidence)):
            raise DomainError('SCOPE_CHANGED', '确认后的文字与固定样片原始证据不同，需要重新核对外发范围。')
        from app.services.director_guidance import revision as brief_revision
        if brief_revision(db, ep) != 0:
            raise DomainError('SCOPE_CHANGED', '固定技术样片不包含后来补充的导演需求，请改用对应授权范围的测试。')
        evidence = [{key: row[key] for key in ('id', 'kind', 'asset_id', 'start', 'end', 'note') if key in row} for row in ep.evidence]
        return [evidence, {ASSET_ID: 8.0}, BACKGROUND]


def report(repo):
    with repo.factory() as db:
        ep = repo.owned(db, Episode, EPISODE_ID, OWNER)
        jobs = list(db.scalars(select(Job).where(Job.episode_id == EPISODE_ID)))
        calls = []
        for call in db.scalars(select(ModelCall).where(ModelCall.episode_id == EPISODE_ID).order_by(ModelCall.created_at)):
            ledger = db.get(Budget, call.reservation_id)
            calls.append({'time': call.created_at, 'kind': call.kind, 'state': call.state,
                          'elapsed_seconds': call.elapsed, 'usage': call.usage, 'ledger_state': ledger.state,
                          'settled_yuan': ledger.cost / 1e6 if ledger.cost is not None else None})
        # Report cloud results from the fixed input, never later user-edited notes.
        checkpoints = {j.kind: j.payload.get('steps', {}) for j in jobs}
        value = {'scope': SCOPE, 'episode_id': ep.id, 'asset_id': ASSET_ID,
                 'url': f'http://127.0.0.1:3000/episodes/{ep.id}?step=direction',
                 'analysis_state': ep.analysis_state, 'evidence_confirmed': ep.evidence_confirmed_revision == ep.evidence_revision,
                 'jobs': [{'id': j.id, 'state': j.state, 'kind': j.kind, 'error': j.error} for j in jobs],
                 'checkpoints': checkpoints, 'calls': calls, 'budget': repo.budget(db, ep.id),
                 'acceptance': '固定样片真实分析，人工核对/选方向/旁白确认/完整出片待完成'}
    directory = repo.data_dir/'技术测试/完整出片'
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=directory, delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        temporary = Path(f.name)
    os.replace(temporary, directory/'技术调用记录.json')
    return value


class FixturePipeline(Pipeline):
    def finish(self, db, job, result, label):
        if job.kind == 'analysis':
            ep = self.repo.owned(db, Episode, EPISODE_ID, OWNER)
            # Fingerprint and publication must commit together for crash recovery.
            job.payload = {**job.payload, 'synthetic_evidence_digest': digest(ep.evidence)}
        super().finish(db, job, result, label)


def analyze(repo, keys, *, authorized=False):
    services = Services(repo, keys, authorized=authorized)
    pipeline = FixturePipeline(repo, services)
    pipeline.require_ready()
    with repo.transaction() as db:
        ep = repo.owned(db, Episode, EPISODE_ID, OWNER)
        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE+':analysis')).first()
        if job:
            if job.payload.get('model_profile') != services.profile():
                raise DomainError('SCOPE_CHANGED', '样片配置已改变，不重复调用。')
            if job.state == 'succeeded':
                return report(repo)
            calls = list(db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)))
            if calls:
                complete = {'vision', 'asr:' + ASSET_ID}.issubset(job.payload.get('steps', {}))
                settled = all(c.state == 'succeeded' and db.get(Budget, c.reservation_id).state == 'settled'
                              and db.get(Budget, c.reservation_id).cost is not None for c in calls)
                if not complete or not settled:
                    raise DomainError('PROBE_NEEDS_REVIEW', '已有未完成调用，先核对费用与原因，不自动重发。')
            job.state = 'running'
        else:
            job = Job(owner_id=OWNER, episode_id=EPISODE_ID, kind='analysis', state='running',
                idempotency_key=SCOPE+':analysis', payload={'asset_ids': [ASSET_ID], 'revision': ep.evidence_revision,
                                                        'model_profile': services.profile(), 'steps': {}})
            db.add(job); db.flush()
            repo.audit(db, OWNER, 'synthetic_test_authorized', job.id, scope=SCOPE,
                       approval_date='2026-11-12', retention_verified=False)
        jid = job.id
    try:
        pipeline.analyze(jid)
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '固定样片分析未完成'
            job.error = {'code': exc.code if isinstance(exc, (DomainError, SpeechError, DirectorError)) else 'PROBE_FAILED',
                         'message': exc.message if isinstance(exc, (DomainError, SpeechError, DirectorError)) else '本机处理未完成。'}
        report(repo)
        raise
    return report(repo)


def directions(repo, keys, *, authorized=False):
    services = Services(repo, keys, authorized=authorized, phase='directions')
    pipeline = Pipeline(repo, services)
    pipeline.require_ready()
    profile = services.profile()
    with repo.transaction() as db:
        ep = repo.owned(db, Episode, EPISODE_ID, OWNER)
        job = db.scalars(select(Job).where(Job.owner_id == OWNER, Job.idempotency_key == SCOPE+':directions')).first()
        if job:
            if job.payload.get('model_profile') != profile or job.payload['revision'] != ep.evidence_revision:
                raise DomainError('SCOPE_CHANGED', '编导依据或配置已改变，不重复调用。')
            from app.services.director_guidance import assert_revision
            assert_revision(db, ep, job.payload.get('brief_revision', 0))
            if job.state == 'succeeded':
                return report(repo)
            if db.scalars(select(ModelCall).where(ModelCall.job_id == job.id)).first():
                raise DomainError('PROBE_NEEDS_REVIEW', '已有编导调用未完成，先核对费用与原因。')
            job.state = 'running'
        else:
            job = Job(owner_id=OWNER, episode_id=EPISODE_ID, kind='directions', state='running',
                idempotency_key=SCOPE+':directions', payload={'asset_ids': [ASSET_ID], 'revision': ep.evidence_revision,
                                                          'brief_revision': 0, 'model_profile': profile, 'steps': {}})
            db.add(job); db.flush()
        jid = job.id
    try:
        pipeline.directions(jid)
    except Exception as exc:
        with repo.transaction() as db:
            job = db.get(Job, jid)
            job.state, job.stage = 'failed', '固定样片编导未完成'
            job.error = {'code': exc.code if isinstance(exc, (DomainError, DirectorError)) else 'PROBE_FAILED',
                         'message': exc.message if isinstance(exc, (DomainError, DirectorError)) else '本机处理未完成。'}
        report(repo)
        raise
    return report(repo)


@contextmanager
def locked(repo):
    with (repo.data_dir/'.synthetic-chain.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DomainError('PROBE_RUNNING', '已有固定样片测试运行中。') from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'analyze', 'directions', 'report'))
    args = parser.parse_args(argv)
    repo = None
    try:
        values = read_configuration(PRODUCT/'backend/.env')
        root = Path(values.get('APP_DATA_DIR') or PRODUCT/'data').resolve()
        if 'demo' in root.name.lower() or root.is_relative_to((PRODUCT/'data/demo').resolve()):
            raise DomainError('DEMO_NOT_ALLOWED', '不能使用演示目录进行真实测试。')
        repo = Repository(root)
        with locked(repo):
            if args.action == 'prepare':
                prepare(repo); result = report(repo)
            elif args.action == 'analyze':
                result = analyze(repo, values, authorized=True)
            elif args.action == 'directions':
                result = directions(repo, values, authorized=True)
            else:
                result = report(repo)
            print(json.dumps({k: result[k] for k in ('url', 'analysis_state', 'evidence_confirmed', 'jobs', 'budget', 'calls')}, ensure_ascii=False))
        return 0
    except (DomainError, SpeechError, DirectorError) as exc:
        print(exc.code + '：' + exc.message)
        return 2
    except (Exception, KeyboardInterrupt):
        print('技术测试未完成；如已发送，费用预留继续占用，不自动重试。')
        return 2
    finally:
        if repo:
            repo.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
