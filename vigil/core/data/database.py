import logging
import hashlib
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, Callable
from peewee import *

from .events import bus

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

    Queued writes are additionally batched: rather than committing on every
    single insert, the thread accumulates whatever arrives within
    `batch_window` seconds and commits them as one transaction — fewer commit
    round-trips under load, and (see DataBus) the batch's event-type tags are
    what DataBus notifies subscribed widgets with once the commit lands. That
    makes `batch_window` a direct latency floor for the UI as well as a
    durability trade-off: a crash can lose the batch still in memory, same as
    before.
    """
    def __init__(self, batch_window: float = 1.0):
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self.batch_window = batch_window
        # When True, submit() executes inline instead of queueing. Used by tests
        # so a write is immediately visible to the following read.
        self.synchronous = False

    def start(self):
        if self.synchronous or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="vigil-db-writer", daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None], event: Optional[str] = None):
        """
        Enqueue a write. Returns immediately; the writer thread executes it.

        `event` is the DataBus event type this write should notify on once
        its batch commits (e.g. 'status', 'metric', 'event', 'log_line').
        Left as None for writes nothing subscribes to (settings, pruning).
        """
        if self.synchronous:
            with db.connection_context():
                fn()
            return
        self._q.put((fn, event))

    def flush(self, timeout: Optional[float] = None):
        """Block until all currently-queued writes have been executed."""
        self._q.join()

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:  # sentinel (unused today; kept for clean shutdown)
                self._q.task_done()
                break
            fn, event = item

            # Collect whatever else arrives within batch_window so the whole
            # group commits once, instead of once per write.
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
                # This thread gets its own connection via connection_context();
                # WAL lets it write while reader threads (web UI) read on theirs.
                with db.connection_context():
                    with db.atomic():
                        for item_fn, _ in batch:
                            try:
                                item_fn()
                            except Exception as e:
                                logging.error(f"DB write failed: {e}")
                # Only after the commit above actually lands: a reader on
                # another connection cannot see this batch's rows before
                # this point (true even in WAL mode), so notifying any
                # earlier would have widgets re-query and find nothing new.
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
    # Unique id of the monitor that wrote this, for per-plugin views. The
    # message also carries a "[Display Name] " prefix, but names repeat across
    # groups so the prefix cannot identify the writer on its own. Nullable for
    # rows written before this column existed, and for core (non-plugin) events.
    source_id = CharField(null=True, index=True)

class Setting(BaseModel):
    """Model for storing persistent key-value settings."""
    key = CharField(primary_key=True)
    value = TextField()

class StatusHistory(BaseModel):
    """Model for tracking the historical state of monitors."""
    timestamp = DateTimeField(default=datetime.now, index=True)
    collector_id = CharField(index=True)
    state = CharField()  # 'online', 'warning', 'failed', 'offline'

class Job(BaseModel):
    """
    A long-running command started on a target host on the user's behalf.

    Distinct from a polling cycle, which is short and repeats: a job is a
    one-shot operation (a borg backup, a repo check) that can run for hours and
    must outlive the browser session that started it. State is persisted here
    rather than held in memory so the UI can reattach after a reload, a restart,
    or from a second browser — the DB row is the single source of truth for
    "is this still running?".

    `state` is one of:
      running   — started, process alive
      succeeded — exited 0
      failed    — exited non-zero (see exit_code / error)
      cancelled — killed on request
    Terminal states all have `finished` set; `running` never does.
    """
    plugin_id = CharField(index=True)     # Which monitor owns this job
    target = CharField(index=True)
    kind = CharField(index=True)          # e.g. 'backup', 'check', 'prune'
    state = CharField(index=True, default='running')
    command = TextField()                 # Redacted — never store raw secrets
    started = DateTimeField(default=datetime.now, index=True)
    finished = DateTimeField(null=True)
    exit_code = IntegerField(null=True)
    # Latest human-readable progress line, replaced as the job advances, so the
    # UI can show current state without scanning the whole output log.
    progress = TextField(null=True)
    error = TextField(null=True)


class JobOutput(BaseModel):
    """
    A single line of stdout/stderr from a Job, in emission order.

    Kept out of the Job row so output can stream in incrementally while the job
    runs, and so the UI can page through it. `seq` orders lines within a job:
    timestamps collide at sub-second resolution when borg emits a burst, and
    the autoincrement id is global rather than per-job.
    """
    job = ForeignKeyField(Job, backref='output', on_delete='CASCADE', index=True)
    seq = IntegerField()
    timestamp = DateTimeField(default=datetime.now)
    stream = CharField(default='stdout')  # 'stdout' | 'stderr'
    message = TextField()

    class Meta:
        indexes = ((('job', 'seq'), True),)


class PluginSnapshot(BaseModel):
    """
    Latest row-level data snapshot for a monitor, as one JSON blob.

    Some plugins collect more than a handful of scalar metrics — a process
    list, a systemd unit list — where every row (PID, unit name, state) is
    itself meaningful and needed by a per-row UI (a table, per-row action
    buttons). Metric is the wrong shape for that: it stores one named number
    per row, which would mean one row *per process per poll* to reconstruct
    a table, and still couldn't carry non-numeric fields like a unit's
    "enabled" string.

    In the collector/web process split, this is also the only way that data
    reaches the web process at all: unlike a single-process design, where a
    plugin's render_ui() could simply read an in-memory list its own
    on_collect() populated, here render_ui() runs in a different process
    from on_collect() entirely. Without a durable, readable snapshot, that
    data is invisible to the web process — see processes.py and
    service_list.py, both of which write one here (via
    InternalDatabaseLogger.snapshot) so their per-row tables and per-row
    actions work.

    One row per plugin_id (upsert, not append) — this is *latest state*, not
    history; Metric/StatusHistory already cover trends over time.
    """
    plugin_id = CharField(primary_key=True)
    updated = DateTimeField(default=datetime.now)
    data = TextField()  # JSON-encoded list/dict — shape is the plugin's own business


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
    # Unique id of the monitor that collected this line (not its display name,
    # which repeats across groups). Also part of the dedup identity below.
    source = CharField(index=True)
    level = CharField()
    message = TextField()
    # sha1 of (target, source, log_time, message) — stable identity of a line.
    dedup_hash = CharField(unique=True)

# DATABASE HELPERS ##################################################################################################################################
def _job_to_dict(job: 'Job') -> Dict[str, Any]:
    """
    Flatten a Job row into a plain dict for UI/API consumers.

    Returning dicts (rather than model instances) keeps callers free of an open
    DB connection and of peewee itself, matching recent_events()/latest_metrics().
    `duration` is computed here because both the UI and the API want elapsed
    time, and a running job has no `finished` to subtract from.
    """
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
    """
    A helper class for plugins to write logs and metrics back to the database.

    `plugin_name` is the display name, used to prefix event messages so the
    global feed reads naturally. `plugin_id` is the monitor's unique id and is
    what rows are keyed and queried by — display names repeat across groups
    (several monitors are called "On Disk"), so anything keyed on the name
    alone mixes their data together. It defaults to the name for callers that
    have no separate id.
    """
    def __init__(self, db_manager: 'DatabaseManager', target: str, plugin_name: str,
                 plugin_id: Optional[str] = None):
        self.db = db_manager
        self.target = target
        self.plugin_name = plugin_name
        self.plugin_id = plugin_id or plugin_name

    def write(self, message: str, level: str = "INFO"):
        """Writes a formatted log entry into the Event table."""
        # Prefix the message with the plugin name for global context; the id
        # is stored alongside so per-plugin views can filter unambiguously.
        formatted_message = f"[{self.plugin_name}] {message}"
        self.db.insert_event(level, formatted_message, self.target,
                             source_id=self.plugin_id)

    def metric(self, name: str, value: float, metadata: Optional[str] = None):
        """Writes a metric entry into the Metric table."""
        self.db.insert_metric(self.target, self.plugin_id, name, value, metadata)

    def log_line(self, message: str, level: str = "INFO", log_time: Optional[str] = None):
        """
        Persist a raw log line collected from the target, deduplicated.

        `log_time` should be the line's own timestamp (e.g. the journald
        timestamp) when available — it makes the dedup identity stable across
        cycles so the same line is stored exactly once. The write is queued to
        the background writer, so this returns immediately.
        """
        # Keyed by id, like metrics and events: display names repeat across
        # groups, and here the collision is twofold — a name-filtered panel
        # would show another monitor's lines, and `source` feeds the dedup
        # hash, so two same-named monitors emitting an identical line would
        # collapse it to a single row and one of them would lose it entirely.
        self.db.insert_log_line(self.target, self.plugin_id, level, message, log_time)

    def snapshot(self, rows: Any):
        """
        Persist this plugin's current row-level data (e.g. a process list, a
        systemd unit list) as its latest snapshot, for the web process's
        render_ui() to read back via UIPlugin.latest_snapshot(). See
        PluginSnapshot's docstring for why this exists — Metric cannot carry
        a table's worth of per-row, non-numeric fields.

        `rows` is any JSON-serializable value (typically a list of dicts,
        one per row); this method owns the json.dumps, callers just pass
        plain Python data.
        """
        import json
        self.db.set_snapshot(self.plugin_id, json.dumps(rows))

# DATABASE MANAGER ##################################################################################################################################
class DatabaseManager:
    """Manages the Peewee ORM connection and schema for Vigil."""
    def __init__(self, db_path: str = "vigil.db", write_batch_seconds: float = 1.0):
        self.db_path = db_path
        _writer.batch_window = write_batch_seconds
        self._connect_and_init()
        self._statuses_cache: Optional[Dict[str, str]] = None
        self._statuses_cache_at: float = 0.0
        self._metric_cache: Dict[tuple, Any] = {}
        self._metric_cache_at: Dict[tuple, float] = {}
        # Generic keyed TTL cache for every other read the dashboard repeats
        # every tick — recent_events, LogLine/Event feeds, metric history,
        # status history. See _cached() below for why one cache covers all
        # of these instead of a dedicated pair of dicts per query shape.
        self._read_cache: Dict[tuple, Any] = {}
        self._read_cache_at: Dict[tuple, float] = {}

    def _cached(self, key: tuple, max_age: float, fetch: Callable[[], Any]) -> Any:
        """
        Return `fetch()`'s result, reused for `max_age` seconds per unique `key`.

        The dashboard's overview page, every plugin detail page, and every
        expanded child inside a group each poll roughly once a second, and
        several of those often want the exact same rows (two tabs open on
        the same monitor, a group with the same metric shown in a card and a
        chart). Without this, each of those is its own SQLite round-trip;
        with it, the first caller in a ~1s window pays for the query and
        the rest reuse its result. `max_age` should not be set below the
        writer's batch window (DEFAULT_WRITE_BATCH_SECONDS) — polling faster
        than a write can land doesn't see fresher data anyway.

        Mirrors latest_statuses()'s original single-purpose cache, widened
        to a keyed dict so every parameterized query (limit, filters, ids)
        gets its own slot instead of each needing a bespoke pair of fields.
        """
        now = time.monotonic()
        cached_at = self._read_cache_at.get(key)
        if cached_at is not None and (now - cached_at) < max_age:
            return self._read_cache[key]
        result = fetch()
        self._read_cache[key] = result
        self._read_cache_at[key] = now
        return result

    def _connect_and_init(self):
        """Connects to the database and creates tables if they don't exist."""
        try:
            # Pragmas are applied to every connection peewee opens (it keeps one
            # connection per thread). Tuned to lean on RAM/cache over disk:
            #   journal_mode=WAL    writes append to a log and fsync at
            #                       checkpoints, not per commit — and let readers
            #                       run concurrently with the writer.
            #   synchronous=OFF     SQLite issues no fsync/fdatasync at all; the
            #                       OS write() returns as soon as the page cache
            #                       has it. This is what actually removes commit
            #                       latency (WAL alone still fsyncs at
            #                       checkpoints). Trade-off: a process crash is
            #                       still safe (WAL's own recovery covers it),
            #                       but an OS crash or power loss around a
            #                       checkpoint can corrupt the WAL/lose recent
            #                       writes — accepted here, same as the
            #                       existing async-writer/batching trade-off,
            #                       for monitoring data that isn't reasonable
            #                       to fsync per row.
            #   cache_size=-262144  256 MB page cache in memory (negative =
            #                       KiB) — large enough that the working set
            #                       for a typical Vigil install stays resident
            #                       and reads rarely touch disk either.
            #   mmap_size=256 MB    memory-map the DB file so reads come from the
            #                       page cache instead of read() syscalls.
            #   temp_store=MEMORY   keep temp tables / sort scratch in RAM.
            #   wal_autocheckpoint  checkpoint less often (bigger WAL, fewer
            #                       fsyncs) — 2000 pages ≈ 8 MB.
            #   busy_timeout        wait for a writer rather than erroring.
            db.init(self.db_path, pragmas={
                'journal_mode': 'wal',
                'synchronous': 0,            # OFF
                'cache_size': -262144,       # 256 MB, in KiB
                'mmap_size': 268435456,      # 256 MB
                'temp_store': 2,             # MEMORY
                'wal_autocheckpoint': 2000,  # pages (~8 MB) between checkpoints
                'busy_timeout': 5000,        # ms
                'foreign_keys': 1,
            })
            db.connect()
            db.create_tables([Metric, Event, Setting, StatusHistory, LogLine, Job, JobOutput, PluginSnapshot])
            self._migrate()
            db.close()  # release the init connection; per-thread ones open on demand
            logging.info(f"Database initialized and connected at {self.db_path}")
        except OperationalError as e:
            logging.error(f"Failed to connect or initialize database at {self.db_path}: {e}")
            raise
        _writer.start()

    @staticmethod
    def _migrate():
        """
        Bring an existing database up to the current schema.

        `create_tables` only creates tables that are missing — it never alters
        one that already exists. A column added to a model therefore appears on
        fresh installs but not on an upgraded database, where every insert then
        fails with "table X has no column named Y". Because writes are queued to
        the background writer, those failures surface only as log lines while
        the data is silently dropped.

        Each step is additive and idempotent, so this is safe to run on every
        start and needs no version bookkeeping.
        """
        columns = {c.name for c in db.get_columns('event')}
        if 'source_id' not in columns:
            # Identifies the monitor that wrote an event; see Event.source_id.
            db.execute_sql('ALTER TABLE event ADD COLUMN source_id VARCHAR(255)')
            db.execute_sql('CREATE INDEX IF NOT EXISTS event_source_id '
                           'ON event (source_id)')
            logging.info("Migrated: added event.source_id")

    def insert_metric(self, target: str, collector: str, metric_name: str, value: float, metadata: Optional[str] = None):
        """Queue a metric record for the background writer (non-blocking)."""
        _writer.submit(lambda: Metric.create(
            target=target, collector=collector, metric_name=metric_name, value=value, metadata=metadata),
            event='metric')

    def insert_status(self, collector_id: str, state: str):
        """Queue a status history record for the background writer (non-blocking)."""
        _writer.submit(lambda: StatusHistory.create(collector_id=collector_id, state=state), event='status')

    def flush(self, timeout: Optional[float] = None):
        """Block until all queued writes have been committed (mainly for tests)."""
        _writer.flush(timeout)

    def latest_statuses(self, max_age: float = 2.0) -> Dict[str, str]:
        """
        Return {collector_id: state} with the most recent status for every
        monitor, in a single query.

        The dashboard renders a status per monitor in several places (tree,
        table, charts), each on its own timer, for every connected browser
        tab. Doing one 'latest row' query per monitor means hundreds of
        sequential SQLite reads at page load — slow, and worse while the
        polling loop holds the write lock. This collapses that to one grouped
        query. Monitors with no status row yet simply won't appear in the map;
        callers treat a missing id as 'offline'.

        Results are cached for `max_age` seconds so the several widgets/tabs
        that all poll this within the same tick share one query instead of
        each hitting SQLite independently; callers already tolerate a few
        seconds of staleness (that's the polling interval itself).
        """
        now = time.monotonic()
        if self._statuses_cache is not None and (now - self._statuses_cache_at) < max_age:
            return self._statuses_cache
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
            result = {row.collector_id: row.state for row in query}
        self._statuses_cache = result
        self._statuses_cache_at = now
        return result

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

    def latest_metric_cached(self, collector: str, metric_name: str, max_age: float = 1.0):
        """
        Return the most recent Metric row for (collector, metric_name), reusing
        a cached result if it was fetched within `max_age` seconds.

        Mirrors latest_statuses()'s cache: the web dashboard can have several
        widgets and browser tabs reading the same plugin's same metric within
        one refresh tick (status_card + a history_chart + a second tab), and
        without this each of those is its own SQLite round-trip. `max_age`
        defaults to the write-batch window for the same reason
        latest_statuses() does — polling faster than a write can land doesn't
        see fresher data anyway.
        """
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
        """
        Return the last `limit` Metric rows for (collector, metric_name),
        oldest first — the series history_chart plots. Cached the same way
        as latest_metric_cached: a group with several children showing the
        same chart, or two open tabs, would otherwise each re-run this.
        """
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
        """Cached recent-metrics feed (all metric names) for metric_table."""
        def _fetch():
            with db.connection_context():
                query = (Metric.select()
                         .where(Metric.collector == collector)
                         .order_by(Metric.timestamp.desc())
                         .limit(limit))
                return [m.__data__ for m in query]
        return self._cached(('collector_metrics', collector, limit), max_age, _fetch)

    def log_lines_cached(self, target: str, filter_prefix: str = '', limit: int = 15, max_age: float = 1.0):
        """Cached LogLine feed for log_table — see metric_history_cached."""
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
        """Cached Event feed for event_table — see metric_history_cached."""
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
        """Cached "last N metrics across every collector" feed for the
        overview page's Recent System Metrics table."""
        def _fetch():
            with db.connection_context():
                query = Metric.select().order_by(Metric.timestamp.desc()).limit(limit)
                return [m.__data__ for m in query]
        return self._cached(('recent_metrics_raw', limit), max_age, _fetch)

    def recent_events_raw_cached(self, limit: int = 20, max_age: float = 1.0):
        """Cached "last N events across every collector" feed for the
        overview page's Recent Events table."""
        def _fetch():
            with db.connection_context():
                query = Event.select().order_by(Event.timestamp.desc()).limit(limit)
                return [e.__data__ for e in query]
        return self._cached(('recent_events_raw', limit), max_age, _fetch)

    def recent_events_cached(self, limit: int = 200, level: Optional[str] = None,
                             target: Optional[str] = None, search: Optional[str] = None,
                             max_age: float = 1.0):
        """Cached wrapper around recent_events() for the dashboard's Events
        page — see recent_events()'s docstring for why the method itself
        stays uncached (the REST API shares it and expects a live read)."""
        key = ('recent_events', limit, level, target, search)
        return self._cached(key, max_age, lambda: self.recent_events(
            limit=limit, level=level, target=target, search=search))

    def latest_status_cached(self, collector_id: str, max_age: float = 1.0):
        """Cached single-collector status lookup for PluginPage.refresh_status."""
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
        """Queue an event record for the background writer (non-blocking)."""
        _writer.submit(lambda: Event.create(level=level, message=message, target=target,
                                            source_id=source_id), event='event')

    def recent_events(self, limit: int = 200, level: Optional[str] = None,
                      target: Optional[str] = None, search: Optional[str] = None):
        """
        Return recent events (newest first) for the unified events feed, with
        optional filtering by level, target host, and message substring.

        Returns a list of plain dicts (not model instances) so callers — the
        events UI and the REST API — can consume it without holding a DB
        connection or importing peewee models. Uncached: the REST API expects
        an on-demand read, not a 1s-stale one. The dashboard's Events page
        caches around this at the call site instead (main_dashboard.py's
        refresh_events) so only the polling UI gets the reuse, not the API.
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
            .execute()), event='log_line')

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

    # JOBS ##########################################################################################################################################
    # Unlike metrics/events, job writes are synchronous. create_job must return a
    # real id to the caller, and a job's output lines must be durable the instant
    # the UI polls for them — queueing either behind the async writer would mean
    # handing back a job that isn't in the DB yet.

    def create_job(self, plugin_id: str, target: str, kind: str, command: str) -> int:
        """Record a newly started job and return its id."""
        with db.connection_context():
            return Job.create(plugin_id=plugin_id, target=target, kind=kind,
                              command=command, state='running').id

    def append_job_output(self, job_id: int, lines, stream: str = 'stdout') -> None:
        """
        Append output lines to a job in one batch.

        Takes an iterable rather than a single line because a running command
        produces output far faster than one-row-per-commit can absorb; the
        reader hands over whatever it has drained since the last flush.
        `seq` continues from the highest existing value so ordering survives
        across batches.
        """
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
        """Replace a job's current progress line (cheap, called frequently)."""
        with db.connection_context():
            Job.update(progress=progress).where(Job.id == job_id).execute()

    def finish_job(self, job_id: int, state: str, exit_code: Optional[int] = None,
                   error: Optional[str] = None) -> None:
        """Mark a job terminal. `state` is succeeded | failed | cancelled."""
        with db.connection_context():
            Job.update(state=state, exit_code=exit_code, error=error,
                       finished=datetime.now()).where(Job.id == job_id).execute()

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Return one job as a plain dict, or None if it does not exist."""
        with db.connection_context():
            job = Job.get_or_none(Job.id == job_id)
            return _job_to_dict(job) if job else None

    def recent_jobs(self, plugin_id: Optional[str] = None, limit: int = 20,
                    kind: Optional[str] = None) -> list:
        """Return recent jobs (newest first) as plain dicts."""
        with db.connection_context():
            query = Job.select().order_by(Job.started.desc())
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            if kind:
                query = query.where(Job.kind == kind)
            return [_job_to_dict(j) for j in query.limit(limit)]

    def running_jobs(self, plugin_id: Optional[str] = None) -> list:
        """
        Return jobs still marked running.

        Used on startup to reconcile: a job whose process died with Vigil is
        still 'running' in the DB, and the UI must not present it as live.
        """
        with db.connection_context():
            query = Job.select().where(Job.state == 'running')
            if plugin_id:
                query = query.where(Job.plugin_id == plugin_id)
            return [_job_to_dict(j) for j in query.order_by(Job.started.desc())]

    def job_output(self, job_id: int, after_seq: int = -1, limit: int = 500) -> list:
        """
        Return a job's output lines in order, after `after_seq`.

        The UI polls with the last seq it has rendered, so each poll transfers
        only new lines rather than re-reading the whole log of a long job.
        """
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
        """
        Fail any job left 'running' by a previous Vigil process.

        Jobs are child processes of Vigil; if it exits, they die with it, but
        their rows still say running. Called once at startup so the UI never
        shows a job as live when nothing is executing it. Returns rows updated.
        """
        with db.connection_context():
            return (Job.update(state='failed', finished=datetime.now(),
                               error='Vigil restarted while this job was running')
                    .where(Job.state == 'running').execute())

    def prune_jobs(self, retention_days: int) -> int:
        """
        Delete finished jobs older than `retention_days` (<=0 disables).

        JobOutput rows cascade via the foreign key. Only terminal jobs are
        considered, so a long-running job is never pruned out from under itself.
        """
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
        return 0  # asynchronous; count is logged by the writer

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a runtime setting from the database."""
        with db.connection_context():
            try:
                return Setting.get(Setting.key == key).value
            except DoesNotExist:
                return default

    def set_setting(self, key: str, value: str):
        """Queue a runtime setting update for the background writer (non-blocking)."""
        _writer.submit(lambda: Setting.insert(key=key, value=value).on_conflict_replace().execute(),
                       event='setting')

    def get_logger(self, target: str, plugin_name: str,
                   plugin_id: Optional[str] = None) -> InternalDatabaseLogger:
        """Factory method to provide a scoped logger for a specific plugin instance."""
        return InternalDatabaseLogger(self, target, plugin_name, plugin_id)

    def set_snapshot(self, plugin_id: str, data: str):
        """
        Queue a plugin's row-level data snapshot for the background writer.

        `data` is pre-serialized JSON (not a Python object) — the caller
        (InternalDatabaseLogger.snapshot) owns the shape, this layer just
        stores text, same as Metric.metadata. Upserts: only the latest
        snapshot per plugin_id is kept.
        """
        _writer.submit(
            lambda: PluginSnapshot.insert(
                plugin_id=plugin_id, data=data, updated=datetime.now()
            ).on_conflict_replace().execute(),
            event='snapshot',
        )

    def get_snapshot(self, plugin_id: str) -> Optional[str]:
        """Return a plugin's latest snapshot JSON, or None if it has never written one."""
        with db.connection_context():
            row = PluginSnapshot.get_or_none(PluginSnapshot.plugin_id == plugin_id)
            return row.data if row else None
