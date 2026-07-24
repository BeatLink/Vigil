import logging
import hashlib
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, Callable
from peewee import *

from .events import bus

db = SqliteDatabase(None)


class _AsyncWriter:
    def __init__(self, batch_window: float = 1.0):
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self.batch_window = batch_window
        self.synchronous = False

    def start(self):
        if self.synchronous or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="vigil-db-writer", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None], event: Optional[str] = None):
        if self.synchronous:
            with db.connection_context():
                fn()
            return
        self._q.put((fn, event))

    def flush(self, timeout: Optional[float] = None):
        self._q.join()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                self._q.task_done()
                break
            fn, event = item

            batch = [(fn, event)]
            deadline = time.monotonic() + self.batch_window
            stop = False
            while not stop:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    nxt = self._q.get(timeout=remaining)
                except queue.Empty:
                    break
                if nxt is None:
                    stop = True
                    self._q.task_done()
                    break
                batch.append(nxt)

            try:
                with db.connection_context():
                    with db.atomic():
                        for item_fn, _ in batch:
                            try:
                                item_fn()
                            except Exception as e:
                                logging.error(f"DB write failed: {e}")
                events_in_batch = {ev for _, ev in batch if ev}
                for ev in events_in_batch:
                    bus.emit(ev)
            finally:
                for _ in batch:
                    self._q.task_done()

            if stop:
                break


_writer = _AsyncWriter()


def flush_writes(timeout: Optional[float] = None):
    _writer.flush(timeout)

class BaseModel(Model):
    class Meta:
        database = db

class Metric(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    collector = CharField()
    metric_name = CharField(index=True)
    value = DoubleField()
    metadata = TextField(null=True)

class Event(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    level = CharField()
    message = TextField()
    target = CharField(null=True)
    source_id = CharField(null=True, index=True)

class Setting(BaseModel):
    key = CharField(primary_key=True)
    value = TextField()

class StatusHistory(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    collector_id = CharField(index=True)
    state = CharField()

class Job(BaseModel):
    plugin_id = CharField(index=True)
    target = CharField(index=True)
    kind = CharField(index=True)
    state = CharField(index=True, default='running')
    command = TextField()
    started = DateTimeField(default=datetime.now, index=True)
    finished = DateTimeField(null=True)
    exit_code = IntegerField(null=True)
    progress = TextField(null=True)
    error = TextField(null=True)


class JobOutput(BaseModel):
    job = ForeignKeyField(Job, backref='output', on_delete='CASCADE', index=True)
    seq = IntegerField()
    timestamp = DateTimeField(default=datetime.now)
    stream = CharField(default='stdout')
    message = TextField()

    class Meta:
        indexes = ((('job', 'seq'), True),)


class PluginSnapshot(BaseModel):
    plugin_id = CharField(primary_key=True)
    updated = DateTimeField(default=datetime.now)
    data = TextField()


class LogLine(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    source = CharField(index=True)
    level = CharField()
    message = TextField()
    dedup_hash = CharField(unique=True)

def _job_to_dict(job: 'Job') -> Dict[str, Any]:
    end = job.finished or datetime.now()
    return {
        'id': job.id,
        'plugin_id': job.plugin_id,
        'target': job.target,
        'kind': job.kind,
        'state': job.state,
        'command': job.command,
        'started': job.started.isoformat(sep=' ', timespec='seconds'),
        'finished': job.finished.isoformat(sep=' ', timespec='seconds') if job.finished else None,
        'duration': max(0, int((end - job.started).total_seconds())),
        'exit_code': job.exit_code,
        'progress': job.progress,
        'error': job.error,
        'running': job.state == 'running',
    }


class InternalDatabaseLogger:
    def __init__(self, db_manager: 'DatabaseManager', target: str, plugin_name: str,
                 plugin_id: Optional[str] = None):
        self.db = db_manager
        self.target = target
        self.plugin_name = plugin_name
        self.plugin_id = plugin_id or plugin_name

    def write(self, message: str, level: str = "INFO"):
        formatted_message = f"[{self.plugin_name}] {message}"
        self.db.insert_event(level, formatted_message, self.target,
                             source_id=self.plugin_id)

    def metric(self, name: str, value: float, metadata: Optional[str] = None):
        self.db.insert_metric(self.target, self.plugin_id, name, value, metadata)

    def log_line(self, message: str, level: str = "INFO", log_time: Optional[str] = None):
        self.db.insert_log_line(self.target, self.plugin_id, level, message, log_time)

    def snapshot(self, rows: Any):
        import json
        self.db.set_snapshot(self.plugin_id, json.dumps(rows))

class DatabaseManager:
    def __init__(self, db_path: str = "vigil.db", write_batch_seconds: float = 1.0):
        self.db_path = db_path
        _writer.batch_window = write_batch_seconds
        self._connect_and_init()
        self._statuses_cache: Optional[Dict[str, str]] = None
        self._statuses_cache_at: float = 0.0
        self._metric_cache: Dict[tuple, Any] = {}
        self._metric_cache_at: Dict[tuple, float] = {}
        self._read_cache: Dict[tuple, Any] = {}
        self._read_cache_at: Dict[tuple, float] = {}

    def _cached(self, key: tuple, max_age: float, fetch: Callable[[], Any]) -> Any:
        now = time.monotonic()
        cached_at = self._read_cache_at.get(key)
        if cached_at is not None and (now - cached_at) < max_age:
            return self._read_cache[key]
        result = fetch()
        self._read_cache[key] = result
        self._read_cache_at[key] = now
        return result

    def _connect_and_init(self):
        try:
            db.init(self.db_path, pragmas={
                'journal_mode': 'wal',
                'synchronous': 0,
                'cache_size': -262144,
                'mmap_size': 268435456,
                'temp_store': 2,
                'wal_autocheckpoint': 2000,
                'busy_timeout': 5000,
                'foreign_keys': 1,
            })
            db.connect()
            db.create_tables([Metric, Event, Setting, StatusHistory, LogLine, Job, JobOutput, PluginSnapshot])
            self._migrate()
            db.close()
            logging.info(f"Database initialized and connected at {self.db_path}")
        except OperationalError as e:
            logging.error(f"Failed to connect or initialize database at {self.db_path}: {e}")
            raise
        _writer.start()

    @staticmethod
    def _migrate():
        columns = {c.name for c in db.get_columns('event')}
        if 'source_id' not in columns:
            db.execute_sql('ALTER TABLE event ADD COLUMN source_id VARCHAR(255)')
            db.execute_sql('CREATE INDEX IF NOT EXISTS event_source_id '
                           'ON event (source_id)')
            logging.info("Migrated: added event.source_id")

    def insert_metric(self, target: str, collector: str, metric_name: str, value: float, metadata: Optional[str] = None):
        _writer.submit(lambda: Metric.create(
            target=target, collector=collector, metric_name=metric_name, value=value, metadata=metadata),
            event='metric')

    def insert_status(self, collector_id: str, state: str):
        _writer.submit(lambda: StatusHistory.create(collector_id=collector_id, state=state), event='status')

    def flush(self, timeout: Optional[float] = None):
        _writer.flush(timeout)

    def latest_statuses(self, max_age: float = 2.0) -> Dict[str, str]:
        now = time.monotonic()
        if self._statuses_cache is not None and (now - self._statuses_cache_at) < max_age:
            return self._statuses_cache
        with db.connection_context():
            newest = (StatusHistory
                      .select(fn.MAX(StatusHistory.id).alias('max_id'))
                      .group_by(StatusHistory.collector_id))
            query = (StatusHistory
                     .select(StatusHistory.collector_id, StatusHistory.state)
                     .where(StatusHistory.id.in_(newest)))
            result = {row.collector_id: row.state for row in query}
        self._statuses_cache = result
        self._statuses_cache_at = now
        return result

    def latest_metrics(self):
        with db.connection_context():
            newest = (Metric
                      .select(fn.MAX(Metric.id).alias('max_id'))
                      .group_by(Metric.collector, Metric.metric_name))
            query = (Metric
                     .select(Metric.target, Metric.collector, Metric.metric_name,
                             Metric.value, Metric.timestamp)
                     .where(Metric.id.in_(newest)))
            return [
                {
                    'target': m.target,
                    'collector': m.collector,
                    'metric_name': m.metric_name,
                    'value': m.value,
                    'timestamp': m.timestamp.isoformat(sep=' ', timespec='seconds'),
                }
                for m in query
            ]

    def latest_metric_cached(self, collector: str, metric_name: str, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                return (
                    Metric.select()
                    .where((Metric.collector == collector) & (Metric.metric_name == metric_name))
                    .order_by(Metric.timestamp.desc())
                    .first()
                )
        return self._cached(('metric', collector, metric_name), max_age, _fetch)

    def metric_history_cached(self, collector: str, metric_name: str, limit: int = 30, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                rows = (
                    Metric.select()
                    .where((Metric.collector == collector) & (Metric.metric_name == metric_name))
                    .order_by(Metric.timestamp.desc())
                    .limit(limit)
                )
                return list(reversed(rows))
        return self._cached(('metric_history', collector, metric_name, limit), max_age, _fetch)

    def collector_metrics_cached(self, collector: str, limit: int = 15, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                query = (Metric.select()
                         .where(Metric.collector == collector)
                         .order_by(Metric.timestamp.desc())
                         .limit(limit))
                return [m.__data__ for m in query]
        return self._cached(('collector_metrics', collector, limit), max_age, _fetch)

    def log_lines_cached(self, target: str, filter_prefix: str = '', limit: int = 15, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                condition = (LogLine.target == target)
                if filter_prefix:
                    condition &= (LogLine.source == filter_prefix)
                query = LogLine.select().where(condition).order_by(LogLine.timestamp.desc()).limit(limit)
                return [e.__data__ for e in query]
        return self._cached(('log_lines', target, filter_prefix, limit), max_age, _fetch)

    def plugin_events_cached(self, plugin_id: str = '', prefix: str = '', target: str = '',
                             limit: int = 100, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                if plugin_id:
                    condition = (Event.source_id == plugin_id)
                else:
                    condition = Event.message.startswith(prefix)
                    if target:
                        condition &= (Event.target == target)
                query = (Event.select()
                         .where(condition)
                         .order_by(Event.timestamp.desc())
                         .limit(limit))
                return [
                    {
                        'timestamp': e.timestamp.isoformat(sep=' ', timespec='seconds'),
                        'level': e.level,
                        'message': e.message[len(prefix):] if prefix and e.message.startswith(prefix)
                                   else e.message,
                    }
                    for e in query
                ]
        return self._cached(('plugin_events', plugin_id, prefix, target, limit), max_age, _fetch)

    def recent_metrics_raw_cached(self, limit: int = 20, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                query = Metric.select().order_by(Metric.timestamp.desc()).limit(limit)
                return [m.__data__ for m in query]
        return self._cached(('recent_metrics_raw', limit), max_age, _fetch)

    def recent_events_raw_cached(self, limit: int = 20, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                query = Event.select().order_by(Event.timestamp.desc()).limit(limit)
                return [e.__data__ for e in query]
        return self._cached(('recent_events_raw', limit), max_age, _fetch)

    def recent_events_cached(self, limit: int = 200, level: Optional[str] = None,
                             target: Optional[str] = None, search: Optional[str] = None,
                             max_age: float = 1.0):
        key = ('recent_events', limit, level, target, search)
        return self._cached(key, max_age, lambda: self.recent_events(
            limit=limit, level=level, target=target, search=search))

    def latest_status_cached(self, collector_id: str, max_age: float = 1.0):
        def _fetch():
            with db.connection_context():
                row = (StatusHistory.select()
                       .where(StatusHistory.collector_id == collector_id)
                       .order_by(StatusHistory.timestamp.desc())
                       .first())
                return row.state if row else 'offline'
        return self._cached(('status', collector_id), max_age, _fetch)

    def insert_event(self, level: str, message: str, target: Optional[str] = None,
                     source_id: Optional[str] = None):
        _writer.submit(lambda: Event.create(level=level, message=message, target=target,
                                            source_id=source_id), event='event')

    def recent_events(self, limit: int = 200, level: Optional[str] = None,
                      target: Optional[str] = None, search: Optional[str] = None):
        with db.connection_context():
            query = Event.select().order_by(Event.timestamp.desc())
            if level:
                query = query.where(Event.level == level)
            if target:
                query = query.where(Event.target == target)
            if search:
                query = query.where(Event.message.contains(search))
            return [
                {
                    'timestamp': e.timestamp.isoformat(sep=' ', timespec='seconds'),
                    'level': e.level,
                    'target': e.target or '',
                    'message': e.message,
                }
                for e in query.limit(limit)
            ]

    def insert_log_line(self, target: str, source: str, level: str, message: str,
                        log_time: Optional[str] = None):
        key = f"{target}\x1f{source}\x1f{log_time or ''}\x1f{message}"
        dedup_hash = hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest()
        _writer.submit(lambda: (
            LogLine
            .insert(target=target, source=source, level=level,
                    message=message, dedup_hash=dedup_hash)
            .on_conflict_ignore()
            .execute()), event='log_line')

    def prune_logs(self, retention_days: int) -> int:
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            deleted = LogLine.delete().where(LogLine.timestamp < cutoff).execute()
            if deleted:
                logging.info(f"Pruned {deleted} log line(s) older than {retention_days}d")

        _writer.submit(_do_prune)
        return 0


    def create_job(self, plugin_id: str, target: str, kind: str, command: str) -> int:
        with db.connection_context():
            return Job.create(plugin_id=plugin_id, target=target, kind=kind,
                              command=command, state='running').id

    def append_job_output(self, job_id: int, lines, stream: str = 'stdout') -> None:
        lines = [ln for ln in lines if ln is not None]
        if not lines:
            return
        with db.connection_context():
            start = (JobOutput
                     .select(fn.COALESCE(fn.MAX(JobOutput.seq), -1))
                     .where(JobOutput.job == job_id)
                     .scalar()) + 1
            with db.atomic():
                JobOutput.insert_many([
                    {'job': job_id, 'seq': start + i, 'stream': stream, 'message': ln}
                    for i, ln in enumerate(lines)
                ]).execute()

    def set_job_progress(self, job_id: int, progress: str) -> None:
        with db.connection_context():
            Job.update(progress=progress).where(Job.id == job_id).execute()

    def finish_job(self, job_id: int, state: str, exit_code: Optional[int] = None,
                   error: Optional[str] = None) -> None:
        with db.connection_context():
            Job.update(state=state, exit_code=exit_code, error=error,
                       finished=datetime.now()).where(Job.id == job_id).execute()

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with db.connection_context():
            job = Job.get_or_none(Job.id == job_id)
            return _job_to_dict(job) if job else None

    def recent_jobs(self, plugin_id: Optional[str] = None, limit: int = 20,
                    kind: Optional[str] = None) -> list:
        with db.connection_context():
            query = Job.select().order_by(Job.started.desc())
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            if kind:
                query = query.where(Job.kind == kind)
            return [_job_to_dict(j) for j in query.limit(limit)]

    def running_jobs(self, plugin_id: Optional[str] = None) -> list:
        with db.connection_context():
            query = Job.select().where(Job.state == 'running')
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            return [_job_to_dict(j) for j in query.order_by(Job.started.desc())]

    def job_output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> list:
        with db.connection_context():
            query = (JobOutput
                     .select()
                     .where((JobOutput.job == job_id) & (JobOutput.seq > after_seq))
                     .order_by(JobOutput.seq)
                     .limit(limit))
            return [
                {
                    'seq': o.seq,
                    'timestamp': o.timestamp.isoformat(sep=' ', timespec='seconds'),
                    'stream': o.stream,
                    'message': o.message,
                }
                for o in query
            ]

    def reconcile_orphaned_jobs(self) -> int:
        with db.connection_context():
            return (Job.update(state='failed', finished=datetime.now(),
                               error='Vigil restarted while this job was running')
                    .where(Job.state == 'running').execute())

    def prune_jobs(self, retention_days: int) -> int:
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            deleted = (Job.delete()
                       .where((Job.state != 'running') & (Job.started < cutoff))
                       .execute())
            if deleted:
                logging.info(f"Pruned {deleted} job(s) older than {retention_days}d")

        _writer.submit(_do_prune)
        return 0

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with db.connection_context():
            try:
                return Setting.get(Setting.key == key).value
            except DoesNotExist:
                return default

    def set_setting(self, key: str, value: str):
        _writer.submit(lambda: Setting.insert(key=key, value=value).on_conflict_replace().execute(),
                       event='setting')

    def get_logger(self, target: str, plugin_name: str,
                   plugin_id: Optional[str] = None) -> InternalDatabaseLogger:
        return InternalDatabaseLogger(self, target, plugin_name, plugin_id)

    def set_snapshot(self, plugin_id: str, data: str):
        _writer.submit(
            lambda: PluginSnapshot.insert(
                plugin_id=plugin_id, data=data, updated=datetime.now()
            ).on_conflict_replace().execute(),
            event='snapshot',
        )

    def get_snapshot(self, plugin_id: str) -> Optional[str]:
        with db.connection_context():
            row = PluginSnapshot.get_or_none(PluginSnapshot.plugin_id == plugin_id)
            return row.data if row else None
