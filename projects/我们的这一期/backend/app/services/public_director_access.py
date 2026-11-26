"""Text dialogue for an explicitly authorized public test episode.

A separate local grant binds the unchanged frame-review grant. Enabling text
does not invalidate earlier observations or enable other provider operations.
The existing scoped transport enforces payload integrity and persisted budgets.
"""
import json
import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select

from ..models import Episode, Budget, ModelCall, Job
from ..chat_models import DirectorChatTurn
from ..repository import DomainError
from .director import DirectorError, _invalid_constant, _unique_object
from .director_chat_access import ScopedDirectorChat, _digest, _stop
from . import review_grant


def grant_path(root, episode_id):
    if not isinstance(episode_id, str) or not review_grant.ID.fullmatch(episode_id):
        raise DomainError('GRANT_INVALID', '本期导演授权标识无效。', 409)
    return root / '授权记录' / f'public-director-{episode_id}.json'


def present(repo, context):
    episode_id = (context.get('episode') or {}).get('id')
    if not isinstance(episode_id, str) or not review_grant.ID.fullmatch(episode_id):
        return False
    path = grant_path(repo.data_dir, episode_id)
    return path.exists() or path.is_symlink()


class PublicDirectorChat(ScopedDirectorChat):
    def ready(self):
        try:
            self._guard()
            grant = self._scope(full=False)
            if grant.get('window'):
                self._window_usage(grant, new_request=True)
            return True, '本期公共测试素材可与导演对话；修改建议需确认后执行。'
        except (DomainError, DirectorError) as exc:
            return False, exc.message

    def _scope(self, *, full):
        try:
            episode_id = self._context['episode']['id']
            path = grant_path(self._repo.data_dir, episode_id)
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 20_000:
                raise ValueError('path')
            grant = json.loads(path.read_text(), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            if (grant['scope'] != 'public-fixture-director-text-v1'
                    or grant['episode_id'] != episode_id or grant['owner_id'] != 'local-owner'
                    or grant['providers'] != ['deepseek'] or grant['operations'] != ['director_chat']
                    or grant['task_limit_yuan'] != 3 or grant['total_limit_yuan'] != 30
                    or grant['retention_verified'] is not False
                    or not isinstance(grant['approval'], str) or not grant['approval'].strip()
                    or not isinstance(grant['approved_at'], str) or not grant['approved_at'].strip()):
                raise ValueError('scope')
            once = grant.get('one_request')
            if once is not None:
                if (not isinstance(once, dict) or not isinstance(once.get('client_id'), str)
                        or not 1 <= len(once['client_id']) <= 128
                        or not review_grant.ID.fullmatch(once.get('version_id', ''))
                        or not review_grant.HASH.fullmatch(once.get('question_sha256', ''))
                        or once.get('max_reservation_yuan') != .09):
                    raise ValueError('one request')
                if 'question' in self._context and (
                        hashlib.sha256(self._context['question'].encode()).hexdigest() != once['question_sha256']
                        or (self._context.get('current_version') or {}).get('id') != once['version_id']):
                    _stop('REQUEST_NOT_AUTHORIZED', '这次授权仅适用于指定的一条测试问题。')
            # Reuse the exact source/ownership/retention checks from frame review.
            # This read-only validation never upgrades uncertain observations.
            if grant.get('window') is not None:
                self._validate_window(grant)
                if once is not None:
                    raise ValueError('mixed grant')
                review_grant.validate_sources(self._repo, episode_id, grant['owner_id'], grant['assets'])
                source = grant
            else:
                source = review_grant._public(self._repo, episode_id, grant['owner_id'],
                    review_grant.grant_path(self._repo.data_dir, episode_id))
                if grant['review_grant_digest'] != _digest(source):
                    _stop('GRANT_CHANGED', '本期素材授权已变化，未发送导演消息。')
            checksum = _digest(grant)
            if self._grant_digest is not None and self._grant_digest != checksum:
                _stop('GRANT_CHANGED', '本期导演授权已变化，未发送消息。')
            self._grant_digest = checksum
            with self._repo.factory() as db:
                episode = self._repo.owned(db, Episode, episode_id, grant['owner_id'])
                if (episode.analysis_state != 'confirmed' or not episode.evidence
                        or episode.evidence_confirmed_revision != episode.evidence_revision
                        or set(episode.analysis_asset_ids) != {a['id'] for a in source['assets']}):
                    _stop('REVIEW_REQUIRED', '请先确认本期素材概览，再与导演讨论。')
            return grant
        except DomainError as exc:
            _stop(exc.code, exc.message)
        except (OSError, KeyError, TypeError, ValueError, AttributeError, RecursionError):
            _stop('GRANT_INVALID', '本期公共素材的导演对话授权缺失或不完整。')

    async def _check_budget(self, reservation, usage):
        await super()._check_budget(reservation, usage)
        grant = self._scope(full=True)
        if grant.get('window'):
            self._window_usage(grant, reservation=reservation)
        once = grant.get('one_request')
        if not once:
            return
        # Bind the one authorized question to its durable job and unique ledger.
        # No second client ID, repeated attempt, or larger hold may send a POST.
        with self._repo.factory() as db:
            calls = list(db.scalars(select(ModelCall).where(ModelCall.reservation_id == reservation)))
            if len(calls) != 1:
                _stop('REQUEST_NOT_AUTHORIZED', '本次文字测试没有唯一的调用记录。')
            call = calls[0]
            job = db.get(Job, call.job_id)
            turn = db.get(DirectorChatTurn, job.payload.get('turn_id')) if job else None
            budget = db.get(Budget, reservation)
            attempts = list(db.scalars(select(ModelCall).where(ModelCall.job_id == call.job_id)))
            if (turn is None or job.kind != 'director_chat' or call.kind != 'director_chat'
                    or turn.client_id != once['client_id'] or turn.context != self._context
                    or turn.episode_id != grant['episode_id'] or turn.owner_id != grant['owner_id']
                    or turn.job_id != job.id or len(attempts) != 1
                    or call.state not in ('prepared', 'sending') or budget is None or budget.amount > 90_000):
                _stop('REQUEST_NOT_AUTHORIZED', '本次单条文字测试范围或费用预留不符。')

    def _validate_window(self, grant):
        window = grant['window']
        start = datetime.fromisoformat(grant['approved_at'])
        end = datetime.fromisoformat(window['expires_at'])
        if (start.tzinfo is None or end.tzinfo is None
                or window['max_requests'] != 10 or window['max_reservation_yuan'] != .09
                or window['total_reservation_yuan'] != .90
                or window['daily_limits'] != {'2026-11-21': 1, '2026-11-22': 9}
                or end != datetime(2026, 11, 22, 16, tzinfo=timezone.utc)):
            raise ValueError('window')
        if not start <= datetime.now(timezone.utc) < end:
            _stop('GRANT_EXPIRED', '本次面试导演对话窗口已结束，未发送消息。')

    def _window_usage(self, grant, *, reservation=None, new_request=False):
        """Count persistent attempts and holds, including failures and retries."""
        window = grant['window']
        start = datetime.fromisoformat(grant['approved_at'])
        today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        with self._repo.factory() as db:
            calls = [call for call in db.scalars(select(ModelCall).where(
                ModelCall.episode_id == grant['episode_id'], ModelCall.kind == 'director_chat'))
                if datetime.fromisoformat(call.created_at) >= start]
            daily = [call for call in calls if datetime.fromisoformat(call.created_at)
                     .astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat() == today]
            holds = [db.get(Budget, call.reservation_id) for call in calls]
            extra = 1 if new_request else 0
            # Pipeline rechecks readiness after reserving. Allow only the exact
            # in-flight context to use its existing hold, never a second slot.
            if new_request and 'question' in self._context:
                for call in daily:
                    if call.state not in ('prepared', 'sending'):
                        continue
                    job = db.get(Job, call.job_id)
                    turn = db.get(DirectorChatTurn, job.payload.get('turn_id')) if job else None
                    if (job and job.state == 'running' and turn and turn.context == self._context
                            and turn.owner_id == grant['owner_id'] and turn.job_id == job.id):
                        extra = 0
            if (len(calls) + extra > window['max_requests']
                    or len(daily) + extra > window['daily_limits'].get(today, 0)
                    or any(row is None or row.owner_id != grant['owner_id']
                           or row.episode_id != grant['episode_id']
                           or row.amount > 90_000 for row in holds)
                    or sum(row.amount for row in holds if row) + extra * 90_000 > 900_000):
                _stop('DEMO_LIMIT_REACHED', '本次演示的今日次数或费用预留已用完，未发送消息。')
            if new_request:
                return
            current = [call for call in calls if call.reservation_id == reservation]
            if len(current) != 1:
                _stop('REQUEST_NOT_AUTHORIZED', '演示调用缺少唯一费用记录。')
            call = current[0]
            job = db.get(Job, call.job_id)
            turn = db.get(DirectorChatTurn, job.payload.get('turn_id')) if job else None
            if (call.owner_id != grant['owner_id'] or call.state not in ('prepared', 'sending')
                    or turn is None or job.kind != 'director_chat' or turn.context != self._context
                    or turn.episode_id != grant['episode_id'] or turn.owner_id != grant['owner_id']
                    or turn.job_id != job.id):
                _stop('REQUEST_NOT_AUTHORIZED', '演示问题与本期授权不符，未发送消息。')
