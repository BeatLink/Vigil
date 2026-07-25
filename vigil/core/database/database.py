import json
import logging
import hashlib
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, List, Callable, TYPE_CHECKING
from peewee import *

if TYPE_CHECKING:
    from vigil.core.connectors.types import CollectResult

from .models import (
    ALL_MODELS, BaseModel, Event, Job, JobOutput, LogLine, Metric,
    PluginSnapshot, Setting, StatusHistory, db,
)
from .rowtypes import (
    EventDict, EventModelDict, JobDict, JobOutputDict, LogLineModelDict,
    MetricModelDict, MetricRowDict, PluginEventDict,
)

from contextlib import contextmanager


@contextmanager
def _reader():
    """Read-path connection scope that opens the thread-local SQLite connection
    if it's closed but — unlike peewee's ``db.connection_context()`` — does NOT
    close it on exit.

    Reads run on a pool of executor threads (see ui/components.offload), and
    ``connection_context().__exit__`` unconditionally calls ``db.close()``, so
    every read would connect + close a fresh connection on its worker thread.
    peewee's SqliteDatabase keeps connections thread-local, so leaving the
    connection open lets the next read on that same thread reuse the warm one.
    The pooled threads live for the process, so the open connections are freed
    at process exit — nothing to close explicitly."""
    if db.is_closed():
        db.connect()
    yield


class _AsyncWriter:
    def __init__(self, batch_window: float = 1.0):
        self._q: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self.batch_window = batch_window
        self.synchronous = False

    def start(self):
        if self.synchronous or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="vigil-db-writer", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None]):
        if self.synchronous:
            with db.connection_context():
                fn()
            return
        self._q.put(fn)

    def flush(self, timeout: Optional[float] = None):
        self._q.join()

    def _run(self):
        while True:
            fn = self._q.get()
            if fn is None:
                self._q.task_done()
                break

            batch = [fn]
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
                        for item_fn in batch:
                            try:
                                item_fn()
                            except Exception as e:
                                logging.error(f"DB write failed: {e}")
            finally:
                for _ in batch:
                    self._q.task_done()

            if stop:
                break


_writer = _AsyncWriter()


def flush_writes(timeout: Optional[float] = None):
    _writer.flush(timeout)


def _job_to_dict(job: 'Job') -> JobDict:
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
        'pid': job.pid,
        'workdir': job.workdir,
        'output_seq': job.output_seq or 0,
    }


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
            db.create_tables(ALL_MODELS)
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

        # Detached-on-target job execution (pid/workdir/output_seq).
        job_columns = {c.name for c in db.get_columns('job')}
        for col, ddl in (
            ('pid', 'ALTER TABLE job ADD COLUMN pid INTEGER'),
            ('workdir', 'ALTER TABLE job ADD COLUMN workdir TEXT'),
            ('output_seq', 'ALTER TABLE job ADD COLUMN output_seq INTEGER DEFAULT 0'),
        ):
            if col not in job_columns:
                db.execute_sql(ddl)
                logging.info(f"Migrated: added job.{col}")

        # Composite index for the hot metric read path (collector + metric_name
        # filter, timestamp ordering). create_tables() adds it on fresh DBs; this
        # backfills it on databases created before the index existed. The name
        # matches peewee's generated name so the two paths converge on one index.
        metric_indexes = {idx.name for idx in db.get_indexes('metric')}
        if 'metric_collector_metric_name_timestamp' not in metric_indexes:
            db.execute_sql(
                'CREATE INDEX IF NOT EXISTS metric_collector_metric_name_timestamp '
                'ON metric (collector, metric_name, timestamp)')
            logging.info("Migrated: added composite index on metric "
                         "(collector, metric_name, timestamp)")

    def insert_metric(self, target: str, collector: str, metric_name: str, value: float, metadata: Optional[str] = None):
        _writer.submit(lambda: Metric.create(
            target=target, collector=collector, metric_name=metric_name, value=value, metadata=metadata))

    def insert_status(self, collector_id: str, state: str):
        _writer.submit(lambda: StatusHistory.create(collector_id=collector_id, state=state))

    def flush(self, timeout: Optional[float] = None):
        _writer.flush(timeout)

    def latest_statuses(self, max_age: float = 2.0) -> Dict[str, str]:
        now = time.monotonic()
        if self._statuses_cache is not None and (now - self._statuses_cache_at) < max_age:
            return self._statuses_cache
        with _reader():
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

    def latest_metrics(self) -> List[MetricRowDict]:
        with _reader():
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
            with _reader():
                return (
                    Metric.select()
                    .where((Metric.collector == collector) & (Metric.metric_name == metric_name))
                    .order_by(Metric.timestamp.desc())
                    .first()
                )
        return self._cached(('metric', collector, metric_name), max_age, _fetch)

    def metric_history_cached(self, collector: str, metric_name: str, limit: int = 30, max_age: float = 1.0):
        def _fetch():
            with _reader():
                rows = (
                    Metric.select()
                    .where((Metric.collector == collector) & (Metric.metric_name == metric_name))
                    .order_by(Metric.timestamp.desc())
                    .limit(limit)
                )
                return list(reversed(rows))
        return self._cached(('metric_history', collector, metric_name, limit), max_age, _fetch)

    def collector_metrics_cached(self, collector: str, limit: int = 15, max_age: float = 1.0) -> List[MetricModelDict]:
        def _fetch():
            with _reader():
                query = (Metric.select()
                         .where(Metric.collector == collector)
                         .order_by(Metric.timestamp.desc())
                         .limit(limit))
                return [m.__data__ for m in query]
        return self._cached(('collector_metrics', collector, limit), max_age, _fetch)

    def log_lines_cached(self, target: str, filter_prefix: str = '', limit: int = 15, max_age: float = 1.0) -> List[LogLineModelDict]:
        def _fetch():
            with _reader():
                condition = (LogLine.target == target)
                if filter_prefix:
                    condition &= (LogLine.source == filter_prefix)
                query = LogLine.select().where(condition).order_by(LogLine.timestamp.desc()).limit(limit)
                return [e.__data__ for e in query]
        return self._cached(('log_lines', target, filter_prefix, limit), max_age, _fetch)

    def plugin_events_cached(self, plugin_id: str = '', prefix: str = '', target: str = '',
                             limit: int = 100, max_age: float = 1.0) -> List[PluginEventDict]:
        def _fetch():
            with _reader():
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

    def recent_metrics_raw_cached(self, limit: int = 20, max_age: float = 1.0) -> List[MetricModelDict]:
        def _fetch():
            with _reader():
                query = Metric.select().order_by(Metric.timestamp.desc()).limit(limit)
                return [m.__data__ for m in query]
        return self._cached(('recent_metrics_raw', limit), max_age, _fetch)

    def recent_events_raw_cached(self, limit: int = 20, max_age: float = 1.0) -> List[EventModelDict]:
        def _fetch():
            with _reader():
                query = Event.select().order_by(Event.timestamp.desc()).limit(limit)
                return [e.__data__ for e in query]
        return self._cached(('recent_events_raw', limit), max_age, _fetch)

    def recent_events_cached(self, limit: int = 200, level: Optional[str] = None,
                             target: Optional[str] = None, search: Optional[str] = None,
                             max_age: float = 1.0) -> List[EventDict]:
        key = ('recent_events', limit, level, target, search)
        return self._cached(key, max_age, lambda: self.recent_events(
            limit=limit, level=level, target=target, search=search))

    def latest_status_cached(self, collector_id: str, max_age: float = 1.0):
        def _fetch():
            with _reader():
                row = (StatusHistory.select()
                       .where(StatusHistory.collector_id == collector_id)
                       .order_by(StatusHistory.timestamp.desc())
                       .first())
                return row.state if row else 'offline'
        return self._cached(('status', collector_id), max_age, _fetch)

    def latest_status_time_cached(self, collector_id: str, max_age: float = 1.0):
        """The timestamp of the most recent status row (or None) — for UIs
        that show 'last checked' distinct from the status value itself."""
        def _fetch():
            with _reader():
                row = (StatusHistory.select()
                       .where(StatusHistory.collector_id == collector_id)
                       .order_by(StatusHistory.timestamp.desc())
                       .first())
                return row.timestamp if row else None
        return self._cached(('status_time', collector_id), max_age, _fetch)

    def insert_event(self, level: str, message: str, target: Optional[str] = None,
                     source_id: Optional[str] = None):
        _writer.submit(lambda: Event.create(level=level, message=message, target=target,
                                            source_id=source_id))

    def recent_events(self, limit: int = 200, level: Optional[str] = None,
                      target: Optional[str] = None, search: Optional[str] = None) -> List[EventDict]:
        with _reader():
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
            .execute()))

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

    def prune_metrics(self, retention_days: int) -> int:
        """Delete Metric rows older than the retention window. Unlike logs and
        jobs, metrics are inserted on every poll of every plugin, so without
        this the table (and every dashboard query that scans it) grows without
        bound. 0/None disables pruning (keep forever), matching prune_logs."""
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            deleted = Metric.delete().where(Metric.timestamp < cutoff).execute()
            if deleted:
                logging.info(f"Pruned {deleted} metric(s) older than {retention_days}d")

        _writer.submit(_do_prune)
        return 0

    def prune_status(self, retention_days: int) -> int:
        """Delete StatusHistory rows older than the retention window. Like
        metrics, a status row is written on every poll, so the table grows
        unbounded without pruning. The most recent row per collector is kept
        regardless of age so latest_status* never loses a plugin's current
        state to pruning. 0/None disables (keep forever)."""
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            newest = (StatusHistory
                      .select(fn.MAX(StatusHistory.id))
                      .group_by(StatusHistory.collector_id))
            deleted = (StatusHistory
                       .delete()
                       .where((StatusHistory.timestamp < cutoff)
                              & (StatusHistory.id.not_in(newest)))
                       .execute())
            if deleted:
                logging.info(f"Pruned {deleted} status row(s) older than {retention_days}d")

        _writer.submit(_do_prune)
        return 0


    def create_job(self, plugin_id: str, target: str, kind: str, command: str,
                   workdir: Optional[str] = None) -> int:
        with db.connection_context():
            return Job.create(plugin_id=plugin_id, target=target, kind=kind,
                              command=command, state='running', workdir=workdir).id

    def set_job_pid(self, job_id: int, pid: int) -> None:
        with db.connection_context():
            Job.update(pid=pid).where(Job.id == job_id).execute()

    def bump_job_output_seq(self, job_id: int, new_seq: int) -> None:
        """Persist how far a poll has consumed the target's output file, so the
        next poll only appends newly-arrived bytes/lines."""
        with db.connection_context():
            Job.update(output_seq=new_seq).where(Job.id == job_id).execute()

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

    def get_job(self, job_id: int) -> Optional[JobDict]:
        with _reader():
            job = Job.get_or_none(Job.id == job_id)
            return _job_to_dict(job) if job else None

    def recent_jobs(self, plugin_id: Optional[str] = None, limit: int = 20,
                    kind: Optional[str] = None) -> List[JobDict]:
        with _reader():
            query = Job.select().order_by(Job.started.desc())
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            if kind:
                query = query.where(Job.kind == kind)
            return [_job_to_dict(j) for j in query.limit(limit)]

    def running_jobs(self, plugin_id: Optional[str] = None) -> List[JobDict]:
        with _reader():
            query = Job.select().where(Job.state == 'running')
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            return [_job_to_dict(j) for j in query.order_by(Job.started.desc())]

    def job_output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> List[JobOutputDict]:
        with _reader():
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
        """Jobs run detached on the target and survive a Vigil restart, so a
        'running' row is no longer force-failed here — the owning plugin's next
        poll re-adopts it (pid alive → resume; pid/exit gone → finalize). Only
        rows with no pid recorded (crashed between create_job and launch, so
        nothing is actually running remotely) are failed."""
        with _reader():
            return (Job.update(state='failed', finished=datetime.now(),
                               error='Vigil restarted before this job started on the target')
                    .where((Job.state == 'running') & (Job.pid.is_null())).execute())

    def running_jobs_with_pid(self, plugin_id: Optional[str] = None) -> List[JobDict]:
        """Running jobs that were launched on a target (have a pid), for a
        restarted engine to re-adopt via polling."""
        with _reader():
            query = Job.select().where((Job.state == 'running') & (Job.pid.is_null(False)))
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            return [_job_to_dict(j) for j in query.order_by(Job.started.desc())]

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
        with _reader():
            try:
                return Setting.get(Setting.key == key).value
            except DoesNotExist:
                return default

    def set_setting(self, key: str, value: str):
        _writer.submit(lambda: Setting.insert(key=key, value=value).on_conflict_replace().execute())

    def set_snapshot(self, plugin_id: str, data: str):
        _writer.submit(
            lambda: PluginSnapshot.insert(
                plugin_id=plugin_id, data=data, updated=datetime.now()
            ).on_conflict_replace().execute())

    def get_snapshot(self, plugin_id: str) -> Optional[str]:
        with _reader():
            row = PluginSnapshot.get_or_none(PluginSnapshot.plugin_id == plugin_id)
            return row.data if row else None

    # --- Plugin-scoped write/read surface -----------------------------------
    #
    # A plugin's pure parse_results()/plan_action() returns a single
    # CollectResult; the engine persists it here with one apply_result() call,
    # passing the plugin's identity (which the engine holds) rather than binding
    # it into a per-plugin object. The scoped reads back PluginDataView.

    def write_event(self, target: str, plugin_id: str, plugin_name: str,
                    message: str, level: str = "INFO") -> None:
        """A plugin event, prefixed with the plugin name for the events feed."""
        self.insert_event(level, f"[{plugin_name}] {message}", target,
                          source_id=plugin_id)

    def apply_result(self, target: str, plugin_id: str, plugin_name: str,
                     result: 'CollectResult') -> None:
        """Fan a CollectResult out to the per-datatype writes. The one place
        that translates the plugin-facing CollectResult contract into
        table-level calls."""
        for name, value in result.metrics.items():
            self.insert_metric(target, plugin_id, name, value, result.metadata.get(name))
        for message, level in result.logs:
            self.write_event(target, plugin_id, plugin_name, message, level=level)
        for message, level, log_time in result.log_lines:
            self.insert_log_line(target, plugin_id, level, message, log_time)
        if result.status is not None:
            self.insert_status(plugin_id, result.status)
        if result.snapshot is not None:
            self.set_snapshot(plugin_id, json.dumps(result.snapshot))
        for key, value in result.settings.items():
            self.set_setting(key, value)

    def latest_snapshot(self, plugin_id: str, default: Any = None) -> Any:
        raw = self.get_snapshot(plugin_id)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
