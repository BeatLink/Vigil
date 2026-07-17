import logging
import hashlib
import queue
import threading
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, Callable
from peewee import *

# Initialize a Peewee database instance
db = SqliteDatabase(None)


class _AsyncWriter:
    """
    Single background thread that owns all DB writes.

    The polling loop runs on the asyncio event loop thread; committing to SQLite
    fsyncs, and on ZFS that can block for a noticeable time. Doing it inline
    stalls the whole async web server. Instead, writes are enqueued (non-blocking
    for the caller) and this one thread drains the queue and commits them, so the
    fsync never happens on the event loop. A single writer also means SQLite only
    ever sees one writer, which is exactly what it wants.
    """
    def __init__(self):
        self._q: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        # When True, submit() executes inline instead of queueing. Used by tests
        # so a write is immediately visible to the following read.
        self.synchronous = False

    def start(self):
        if self.synchronous or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="vigil-db-writer", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None]):
        """Enqueue a write. Returns immediately; the writer thread executes it."""
        if self.synchronous:
            with db.connection_context():
                fn()
            return
        self._q.put(fn)

    def flush(self, timeout: Optional[float] = None):
        """Block until all currently-queued writes have been executed."""
        self._q.join()

    def _run(self):
        while True:
            fn = self._q.get()
            if fn is None:  # sentinel (unused today; kept for clean shutdown)
                self._q.task_done()
                break
            try:
                # This thread gets its own connection via connection_context();
                # WAL lets it write while reader threads (web UI) read on theirs.
                with db.connection_context():
                    fn()
            except Exception as e:
                logging.error(f"DB write failed: {e}")
            finally:
                self._q.task_done()


_writer = _AsyncWriter()


def flush_writes(timeout: Optional[float] = None):
    """Block until all queued DB writes have been committed.

    Writes are asynchronous (queued to the background writer), so anything that
    needs to read its own writes back immediately — chiefly tests — must call
    this first.
    """
    _writer.flush(timeout)

# DATABASE MODELS ###################################################################################################################################
class BaseModel(Model):
    class Meta:
        database = db

class Metric(BaseModel):
    """Model for storing collected metrics."""
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    collector = CharField()
    metric_name = CharField(index=True)
    value = DoubleField()
    metadata = TextField(null=True) # For storing JSON or other structured data

class Event(BaseModel):
    """Model for storing system events and logs."""
    timestamp = DateTimeField(default=datetime.now, index=True)
    level = CharField()
    message = TextField()
    target = CharField(null=True)

class Setting(BaseModel):
    """Model for storing persistent key-value settings."""
    key = CharField(primary_key=True)
    value = TextField()

class StatusHistory(BaseModel):
    """Model for tracking the historical state of monitors."""
    timestamp = DateTimeField(default=datetime.now, index=True)
    collector_id = CharField(index=True)
    state = CharField()  # 'online', 'warning', 'failed', 'offline'

class LogLine(BaseModel):
    """
    Persistent storage for log lines collected from targets (e.g. journald).

    Unlike Event (which stores Vigil's own status/threshold messages), this
    table holds raw log output pulled from remote hosts. Because collectors
    re-fetch the last N lines every cycle, the same line arrives repeatedly;
    `dedup_hash` is a UNIQUE key derived from the identity of a line so that
    re-inserting an already-stored line is a no-op (see insert_log_line).
    """
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    source = CharField(index=True)   # Logical source, e.g. plugin name or unit
    level = CharField()
    message = TextField()
    # sha1 of (target, source, log_time, message) — stable identity of a line.
    dedup_hash = CharField(unique=True)

# DATABASE HELPERS ##################################################################################################################################
class InternalDatabaseLogger:
    """A helper class for plugins to write logs and metrics back to the database."""
    def __init__(self, db_manager: 'DatabaseManager', target: str, plugin_name: str):
        self.db = db_manager
        self.target = target
        self.plugin_name = plugin_name

    def write(self, message: str, level: str = "INFO"):
        """Writes a formatted log entry into the Event table."""
        # Prefix the message with the plugin name for global context
        formatted_message = f"[{self.plugin_name}] {message}"
        self.db.insert_event(level, formatted_message, self.target)

    def metric(self, name: str, value: float, metadata: Optional[str] = None):
        """Writes a metric entry into the Metric table."""
        self.db.insert_metric(self.target, self.plugin_name, name, value, metadata)

    def log_line(self, message: str, level: str = "INFO", log_time: Optional[str] = None):
        """
        Persist a raw log line collected from the target, deduplicated.

        `log_time` should be the line's own timestamp (e.g. the journald
        timestamp) when available — it makes the dedup identity stable across
        cycles so the same line is stored exactly once. The write is queued to
        the background writer, so this returns immediately.
        """
        self.db.insert_log_line(self.target, self.plugin_name, level, message, log_time)

# DATABASE MANAGER ##################################################################################################################################
class DatabaseManager:
    """Manages the Peewee ORM connection and schema for Vigil."""
    def __init__(self, db_path: str = "vigil.db"):
        self.db_path = db_path
        self._connect_and_init()

    def _connect_and_init(self):
        """Connects to the database and creates tables if they don't exist."""
        try:
            # Pragmas are applied to every connection peewee opens (it keeps one
            # connection per thread). Tuned to lean on RAM/cache over disk:
            #   journal_mode=WAL    writes append to a log and fsync at
            #                       checkpoints, not per commit — and let readers
            #                       run concurrently with the writer.
            #   synchronous=NORMAL  in WAL, only fsync at checkpoints, not on
            #                       every commit (safe against app crashes; only
            #                       a power loss at a checkpoint risks the last
            #                       few durable writes — fine for monitoring).
            #   cache_size=-65536   64 MB page cache in memory (negative = KiB).
            #   mmap_size=256 MB    memory-map the DB file so reads come from the
            #                       page cache instead of read() syscalls.
            #   temp_store=MEMORY   keep temp tables / sort scratch in RAM.
            #   wal_autocheckpoint  checkpoint less often (bigger WAL, fewer
            #                       fsyncs) — 2000 pages ≈ 8 MB.
            #   busy_timeout        wait for a writer rather than erroring.
            db.init(self.db_path, pragmas={
                'journal_mode': 'wal',
                'synchronous': 1,            # NORMAL
                'cache_size': -65536,        # 64 MB, in KiB
                'mmap_size': 268435456,      # 256 MB
                'temp_store': 2,             # MEMORY
                'wal_autocheckpoint': 2000,  # pages (~8 MB) between checkpoints
                'busy_timeout': 5000,        # ms
                'foreign_keys': 1,
            })
            db.connect()
            db.create_tables([Metric, Event, Setting, StatusHistory, LogLine])
            db.close()  # release the init connection; per-thread ones open on demand
            logging.info(f"Database initialized and connected at {self.db_path}")
        except OperationalError as e:
            logging.error(f"Failed to connect or initialize database at {self.db_path}: {e}")
            raise
        _writer.start()

    def insert_metric(self, target: str, collector: str, metric_name: str, value: float, metadata: Optional[str] = None):
        """Queue a metric record for the background writer (non-blocking)."""
        _writer.submit(lambda: Metric.create(
            target=target, collector=collector, metric_name=metric_name, value=value, metadata=metadata))

    def insert_status(self, collector_id: str, state: str):
        """Queue a status history record for the background writer (non-blocking)."""
        _writer.submit(lambda: StatusHistory.create(collector_id=collector_id, state=state))

    def flush(self, timeout: Optional[float] = None):
        """Block until all queued writes have been committed (mainly for tests)."""
        _writer.flush(timeout)

    def latest_statuses(self) -> Dict[str, str]:
        """
        Return {collector_id: state} with the most recent status for every
        monitor, in a single query.

        The dashboard renders a status per monitor in several places (tree,
        table, charts). Doing one 'latest row' query per monitor means hundreds
        of sequential SQLite reads at page load — slow, and worse while the
        polling loop holds the write lock. This collapses that to one grouped
        query. Monitors with no status row yet simply won't appear in the map;
        callers treat a missing id as 'offline'.
        """
        with db.connection_context():
            # Highest id per collector_id == its most recent row (id is a
            # monotonic rowid, so it breaks timestamp ties deterministically
            # and avoids a second lookup). One grouped subquery + one join.
            newest = (StatusHistory
                      .select(fn.MAX(StatusHistory.id).alias('max_id'))
                      .group_by(StatusHistory.collector_id))
            query = (StatusHistory
                     .select(StatusHistory.collector_id, StatusHistory.state)
                     .where(StatusHistory.id.in_(newest)))
            return {row.collector_id: row.state for row in query}

    def latest_metrics(self):
        """
        Return the most recent value of every (collector, metric_name) pair as a
        list of dicts: {target, collector, metric_name, value, timestamp}.

        One grouped query (max id per collector+metric) + one join, mirroring
        latest_statuses(). Used by the Prometheus exporter and the REST API so a
        scrape/request is a single fast read rather than one query per series.
        """
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

    def insert_event(self, level: str, message: str, target: Optional[str] = None):
        """Queue an event record for the background writer (non-blocking)."""
        _writer.submit(lambda: Event.create(level=level, message=message, target=target))

    def recent_events(self, limit: int = 200, level: Optional[str] = None,
                      target: Optional[str] = None, search: Optional[str] = None):
        """
        Return recent events (newest first) for the unified events feed, with
        optional filtering by level, target host, and message substring.

        Returns a list of plain dicts (not model instances) so callers — the
        events UI and the REST API — can consume it without holding a DB
        connection or importing peewee models.
        """
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
        """
        Queue a raw log line for the background writer, deduplicated by content.

        The dedup key is a hash of (target, source, log_time, message). Because
        collectors re-fetch the same trailing lines each cycle, the UNIQUE
        dedup_hash + on_conflict_ignore turns a re-insert into a no-op — dedup is
        enforced by the DB itself, so no read is needed on the hot path. Queued,
        so it never blocks the caller (use flush() to await it in tests).
        """
        # log_time anchors the identity to the line's own timestamp when the
        # source provides one; without it we fall back to (target, source, msg)
        # so identical repeated lines still collapse to a single row.
        key = f"{target}\x1f{source}\x1f{log_time or ''}\x1f{message}"
        dedup_hash = hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest()
        _writer.submit(lambda: (
            LogLine
            .insert(target=target, source=source, level=level,
                    message=message, dedup_hash=dedup_hash)
            .on_conflict_ignore()
            .execute()))

    def prune_logs(self, retention_days: int) -> int:
        """
        Delete stored log lines older than `retention_days`. A value <= 0
        disables pruning (logs are kept indefinitely). Returns rows deleted.
        """
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            deleted = LogLine.delete().where(LogLine.timestamp < cutoff).execute()
            if deleted:
                logging.info(f"Pruned {deleted} log line(s) older than {retention_days}d")

        _writer.submit(_do_prune)
        return 0  # pruning is asynchronous; count is logged by the writer

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a runtime setting from the database."""
        with db.connection_context():
            try:
                return Setting.get(Setting.key == key).value
            except DoesNotExist:
                return default

    def set_setting(self, key: str, value: str):
        """Queue a runtime setting update for the background writer (non-blocking)."""
        _writer.submit(lambda: Setting.insert(key=key, value=value).on_conflict_replace().execute())

    def get_logger(self, target: str, plugin_name: str) -> InternalDatabaseLogger:
        """Factory method to provide a scoped logger for a specific plugin instance."""
        return InternalDatabaseLogger(self, target, plugin_name)
