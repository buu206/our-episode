from contextlib import contextmanager
from pathlib import Path
import math
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config
from .config import BACKEND
from .models import Audit, Budget, Job, ModelCall, Version


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Repository:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ('assets', 'outputs', 'incoming'):
            (self.data_dir / name).mkdir(exist_ok=True)
        self.engine = create_engine('sqlite:///' + str(self.data_dir / 'app.sqlite3'),
                                    connect_args={'check_same_thread': False, 'timeout': 15})

        @event.listens_for(self.engine, 'connect')
        def sqlite_settings(connection, _):
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA busy_timeout=15000')

        cfg = Config(str(BACKEND / 'alembic.ini'))
        cfg.set_main_option('script_location', str(BACKEND / 'alembic'))
        with self.engine.begin() as connection:
            cfg.attributes['connection'] = connection
            command.upgrade(cfg, 'head')
        self.factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def transaction(self):
        with self.factory() as db:
            # Reserve writes before checks to make quotas/idempotency atomic.
            db.execute(text('BEGIN IMMEDIATE'))
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def owned(db, model, item_id, owner):
        item = db.get(model, item_id)
        if item is None or item.owner_id != owner:
            raise DomainError('NOT_FOUND', '内容不存在或无权访问。', 404)
        return item

    @staticmethod
    def audit(db, owner, action, target, **metadata):
        db.add(Audit(owner_id=owner, action=action, target_id=target, details=metadata))

    def path(self, relative):
        p = (self.data_dir / relative).resolve()
        if not p.is_relative_to(self.data_dir):
            raise DomainError('INVALID_PATH', '文件位置无效。')
        return p

    def budget(self, db, episode_id):
        rows = list(db.scalars(select(Budget)))
        settled = lambda r: r.state == 'settled' and r.cost is not None
        held = lambda r: r.state in ('reserved', 'unknown') or (r.state == 'settled' and r.cost is None)
        spent = sum(r.cost for r in rows if settled(r))
        reserved = sum(r.amount for r in rows if held(r))
        mine = [r for r in rows if r.episode_id == episode_id]
        return {'spent': sum(r.cost for r in mine if settled(r)) / 1e6,
                'reserved': sum(r.amount for r in mine if held(r)) / 1e6,
                'limit': 3, 'total_spent': spent / 1e6, 'total_reserved': reserved / 1e6, 'total_limit': 30}

    def reserve(self, db, owner, episode_id, amount):
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or (isinstance(amount, float) and not math.isfinite(amount)) or amount <= 0:
            raise DomainError('INVALID_AMOUNT', '预算预留必须为有限正数。')
        if amount > 3:
            raise DomainError('BUDGET_EXCEEDED', '费用预留超过本期预算，已停止。', 409)
        units = math.ceil(amount * 1e6)
        b = self.budget(db, episode_id)
        if round((b['spent'] + b['reserved']) * 1e6) + units > 3_000_000 or round((b['total_spent'] + b['total_reserved']) * 1e6) + units > 30_000_000:
            raise DomainError('BUDGET_EXCEEDED', '费用预留超过本期或首批预算，已停止。', 409)
        row = Budget(owner_id=owner, episode_id=episode_id, amount=units)
        db.add(row)
        db.flush()
        return row.id

    def recover(self):
        with self.transaction() as db:
            for job in db.scalars(select(Job).where(Job.state == 'running')):
                job.state, job.stage = 'failed', '上次处理被中断，可重试'
                job.error = {'code': 'INTERRUPTED', 'message': '服务中断；已保存输入，需重试处理阶段。'}
                if job.version_id:
                    v = db.get(Version, job.version_id)
                    if v and v.state in ('rendering', 'voicing'):
                        v.state, v.error = 'failed', job.error
                bid = job.payload.get('reservation')
                if bid:
                    b = db.get(Budget, bid)
                    if b and b.state == 'reserved':
                        b.state = 'unknown'
                self.audit(db, job.owner_id, 'job_interrupted', job.id)
            for call in db.scalars(select(ModelCall).where(ModelCall.state.in_(('prepared', 'sending')))):
                ledger = db.get(Budget, call.reservation_id)
                if call.state == 'prepared':
                    ledger.state, ledger.cost = 'settled', 0
                    call.usage = {'cost_yuan': 0, 'cost_status': 'not_sent', 'real_call': False}
                else:
                    ledger.state = 'unknown'
                    call.usage = {'cost_yuan': None, 'cost_status': 'unknown',
                                  'real_call': call.usage.get('real_call', False)}
                call.state = 'interrupted'
