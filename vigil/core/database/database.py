"""Database Engine — memory-first reads, SQLite as the persistence sink.

The system of record is the in-memory ``StateStore`` (``core/state/``).
Collectors write into it and the UI reads from it; every method here that
returns data serves it from live Python objects, never a query.

SQLite's only two jobs are:
  * ``hydrate()`` — one bulk load at startup, restoring the store from disk.
  * persistence — every write is mirrored to disk on a background thread so
    the state survives a restart.

Nothing in the running system reads from SQLite. That means a slow disk, a
locked database, or a checkpoint stall can no longer make the UI wait, which
is what the old per-read TTL cache existed to paper over. The cache is gone
with the query path that needed it.

``DatabaseManager`` keeps its previous method names and return shapes so the
UI, plugins, and exporters are unchanged by the move — what changed is where
the data comes from.
"""

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

from vigil.core.state import (
    BufferSizes,
    EventRecord,
    JobRecord,
    JobOutputRecord,
    LogLineRecord,
    MetricRecord,
    StateStore,
    StatusRecord,
)

from .models import (
    ALL_MODELS,
    BaseModel,
    Event,
    Job,
    JobOutput,
    LogLine,
    Metric,
    PluginSnapshot,
    Setting,
    StatusHistory,
    db,
)
from .rowtypes import (
    EventDict,
    EventModelDict,
    JobDict,
    JobOutputDict,
    LogLineModelDict,
    MetricModelDict,
    MetricRowDict,
    PluginEventDict,
)

from contextlib import contextmanager


@contextmanager
def _reader():
    """Connection scope for the startup hydration reads, which run once on the
    main thread. Opens the thread-local connection if closed and leaves it
    open — peewee's ``connection_context()`` would close it on exit."""
    if db.is_closed():
        db.connect()
    yield


class _AsyncWriter:
    """Serialises persistence onto one background thread, batching whatever
    arrives inside ``batch_window`` into a single transaction.

    Now that no read path touches SQLite, this thread is the only thing that
    does, and nothing waits on it — writes are submitted and forgotten. The
    batching amortises fsync across a poll cycle's worth of metrics rather
    than paying it per row."""

    def __init__(self, batch_window: float = 1.0):
        self._q: "queue.Queue[Optional[Callable[[], None]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self.batch_window = batch_window
        self.synchronous = False

    def start(self):
        if self.synchronous or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(
            target=self._run, name="vigil-db-writer", daemon=True
        )
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


class DatabaseManager:
    def __init__(
        self,
        db_path: str = "vigil.db",
        write_batch_seconds: float = 1.0,
        buffers: Optional[BufferSizes] = None,
        store: Optional[StateStore] = None,
    ):
        self.db_path = db_path
        self.store = store or StateStore(buffers)
        _writer.batch_window = write_batch_seconds
        self._connect_and_init()
        self.hydrate()

    def _connect_and_init(self):
        try:
            db.init(
                self.db_path,
                pragmas={
                    "journal_mode": "wal",
                    "synchronous": 0,
                    "cache_size": -262144,
                    "mmap_size": 268435456,
                    "temp_store": 2,
                    "wal_autocheckpoint": 2000,
                    "busy_timeout": 5000,
                    "foreign_keys": 1,
                },
            )
            db.connect()
            db.create_tables(ALL_MODELS)
            self._migrate()
            db.close()
            logging.info(f"Database initialized and connected at {self.db_path}")
        except OperationalError as e:
            logging.error(
                f"Failed to connect or initialize database at {self.db_path}: {e}"
            )
            raise
        _writer.start()

    @staticmethod
    def _migrate():
        columns = {c.name for c in db.get_columns("event")}
        if "source_id" not in columns:
            db.execute_sql("ALTER TABLE event ADD COLUMN source_id VARCHAR(255)")
            db.execute_sql(
                "CREATE INDEX IF NOT EXISTS event_source_id " "ON event (source_id)"
            )
            logging.info("Migrated: added event.source_id")

        # Detached-on-target job execution (pid/workdir/output_seq).
        job_columns = {c.name for c in db.get_columns("job")}
        for col, ddl in (
            ("pid", "ALTER TABLE job ADD COLUMN pid INTEGER"),
            ("workdir", "ALTER TABLE job ADD COLUMN workdir TEXT"),
            ("output_seq", "ALTER TABLE job ADD COLUMN output_seq INTEGER DEFAULT 0"),
        ):
            if col not in job_columns:
                db.execute_sql(ddl)
                logging.info(f"Migrated: added job.{col}")

        # Composite index on metric (collector, metric_name, timestamp). The
        # live read path no longer queries SQLite, but startup hydration loads
        # recent history per series, and the retention prunes scan by
        # timestamp — both still benefit.
        metric_indexes = {idx.name for idx in db.get_indexes("metric")}
        if "metric_collector_metric_name_timestamp" not in metric_indexes:
            db.execute_sql(
                "CREATE INDEX IF NOT EXISTS metric_collector_metric_name_timestamp "
                "ON metric (collector, metric_name, timestamp)"
            )
            logging.info(
                "Migrated: added composite index on metric "
                "(collector, metric_name, timestamp)"
            )

    # ------------------------------------------------------------------
    # Startup hydration — the only read path from SQLite
    # ------------------------------------------------------------------
    def hydrate(self) -> None:
        """Restore the store from disk. Runs once, before the engine starts
        polling; after this the process never reads SQLite again.

        Each history stream loads only as much as its buffer holds, so startup
        cost is bounded by the configured buffer sizes rather than by how large
        the database has grown."""
        try:
            with _reader():
                self._hydrate_statuses()
                self._hydrate_metrics()
                self._hydrate_events()
                self._hydrate_log_lines()
                self._hydrate_snapshots()
                self._hydrate_settings()
                self._hydrate_jobs()
            logging.info("State store hydrated from database")
        except OperationalError as e:
            # An unreadable database on a fresh install is not fatal — the
            # store simply starts empty and collectors repopulate it.
            logging.error(f"Failed to hydrate state from {self.db_path}: {e}")

    def _hydrate_statuses(self) -> None:
        newest = StatusHistory.select(fn.MAX(StatusHistory.id).alias("max_id")).group_by(
            StatusHistory.collector_id
        )
        rows = StatusHistory.select().where(StatusHistory.id.in_(newest))
        self.store.statuses.update(
            {
                row.collector_id: StatusRecord(
                    collector_id=row.collector_id,
                    state=row.state,
                    timestamp=row.timestamp,
                )
                for row in rows
            }
        )

    def _hydrate_metrics(self) -> None:
        depth = self.store.buffers.metric_history
        series_keys = Metric.select(Metric.collector, Metric.metric_name).distinct()
        for key in series_keys:
            rows = (
                Metric.select()
                .where(
                    (Metric.collector == key.collector)
                    & (Metric.metric_name == key.metric_name)
                )
                .order_by(Metric.timestamp.desc())
                .limit(depth)
            )
            self.store.load_metrics(
                MetricRecord(
                    target=row.target,
                    collector=row.collector,
                    metric_name=row.metric_name,
                    value=row.value,
                    metadata=row.metadata,
                    timestamp=row.timestamp,
                )
                for row in reversed(list(rows))
            )

    def _hydrate_events(self) -> None:
        rows = (
            Event.select()
            .order_by(Event.timestamp.desc())
            .limit(self.store.buffers.event_history)
        )
        self.store.load_events(
            EventRecord(
                level=row.level,
                message=row.message,
                target=row.target,
                source_id=row.source_id,
                timestamp=row.timestamp,
            )
            for row in reversed(list(rows))
        )

    def _hydrate_log_lines(self) -> None:
        depth = self.store.buffers.log_history
        targets = LogLine.select(LogLine.target).distinct()
        for entry in targets:
            rows = (
                LogLine.select()
                .where(LogLine.target == entry.target)
                .order_by(LogLine.timestamp.desc())
                .limit(depth)
            )
            self.store.load_log_lines(
                LogLineRecord(
                    target=row.target,
                    source=row.source,
                    level=row.level,
                    message=row.message,
                    dedup_hash=row.dedup_hash,
                    timestamp=row.timestamp,
                )
                for row in reversed(list(rows))
            )

    def _hydrate_snapshots(self) -> None:
        for row in PluginSnapshot.select():
            try:
                self.store.snapshots[row.plugin_id] = json.loads(row.data)
            except (ValueError, TypeError):
                logging.warning(f"Discarding unreadable snapshot for {row.plugin_id}")

    def _hydrate_settings(self) -> None:
        self.store.settings.update({row.key: row.value for row in Setting.select()})

    def _hydrate_jobs(self) -> None:
        """Only unfinished jobs and the recent tail are restored — a finished
        job from last week has no live consumer, and the job panel reads the
        most recent handful."""
        rows = Job.select().order_by(Job.started.desc()).limit(200)
        jobs = [
            JobRecord(
                id=row.id,
                plugin_id=row.plugin_id,
                target=row.target,
                kind=row.kind,
                state=row.state,
                command=row.command,
                started=row.started,
                finished=row.finished,
                exit_code=row.exit_code,
                progress=row.progress,
                error=row.error,
                pid=row.pid,
                workdir=row.workdir,
                output_seq=row.output_seq or 0,
            )
            for row in rows
        ]
        output: Dict[int, List[JobOutputRecord]] = {}
        running_ids = [job.id for job in jobs if job.state == "running"]
        if running_ids:
            for row in (
                JobOutput.select()
                .where(JobOutput.job.in_(running_ids))
                .order_by(JobOutput.seq)
            ):
                output.setdefault(row.job_id, []).append(
                    JobOutputRecord(
                        seq=row.seq,
                        stream=row.stream,
                        message=row.message,
                        timestamp=row.timestamp,
                    )
                )
        self.store.load_jobs(jobs, output)

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def insert_metric(
        self,
        target: str,
        collector: str,
        metric_name: str,
        value: float,
        metadata: Optional[str] = None,
    ):
        record = self.store.add_metric(target, collector, metric_name, value, metadata)
        _writer.submit(
            lambda: Metric.create(
                target=record.target,
                collector=record.collector,
                metric_name=record.metric_name,
                value=record.value,
                metadata=record.metadata,
                timestamp=record.timestamp,
            )
        )

    def latest_metric(self, collector: str, metric_name: str) -> Optional[MetricRecord]:
        return self.store.latest_metric(collector, metric_name)

    def metric_history(
        self, collector: str, metric_name: str, limit: int = 30
    ) -> List[MetricRecord]:
        return self.store.metric_history(collector, metric_name, limit=limit)

    def collector_metrics(
        self, collector: str, limit: int = 15
    ) -> List[MetricModelDict]:
        return [m.as_row() for m in self.store.collector_metrics(collector, limit=limit)]

    def latest_metrics(self) -> List[MetricRowDict]:
        return [
            {
                "target": m.target,
                "collector": m.collector,
                "metric_name": m.metric_name,
                "value": m.value,
                "timestamp": m.timestamp.isoformat(sep=" ", timespec="seconds"),
            }
            for m in self.store.latest_metrics()
        ]

    def recent_metrics_raw(self, limit: int = 20) -> List[MetricModelDict]:
        return [m.as_row() for m in self.store.recent_metrics(limit=limit)]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def insert_status(self, collector_id: str, state: str):
        record = StatusRecord(
            collector_id=collector_id, state=state, timestamp=datetime.now()
        )
        self.store.statuses[collector_id] = record
        _writer.submit(
            lambda: StatusHistory.create(
                collector_id=record.collector_id,
                state=record.state,
                timestamp=record.timestamp,
            )
        )

    def latest_statuses(self) -> Dict[str, str]:
        return {cid: rec.state for cid, rec in self.store.statuses.items()}

    def latest_status(self, collector_id: str) -> str:
        record = self.store.statuses.get(collector_id)
        return record.state if record else "offline"

    def latest_status_time(self, collector_id: str):
        record = self.store.statuses.get(collector_id)
        return record.timestamp if record else None

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def insert_event(
        self,
        level: str,
        message: str,
        target: Optional[str] = None,
        source_id: Optional[str] = None,
    ):
        record = self.store.add_event(level, message, target, source_id)
        _writer.submit(
            lambda: Event.create(
                level=record.level,
                message=record.message,
                target=record.target,
                source_id=record.source_id,
                timestamp=record.timestamp,
            )
        )

    def recent_events(
        self,
        limit: int = 200,
        level: Optional[str] = None,
        target: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[EventDict]:
        return [
            {
                "timestamp": e.timestamp.isoformat(sep=" ", timespec="seconds"),
                "level": e.level,
                "target": e.target or "",
                "message": e.message,
            }
            for e in self.store.recent_events(
                limit=limit, level=level, target=target, search=search
            )
        ]

    def recent_events_raw(self, limit: int = 20) -> List[EventModelDict]:
        return [e.as_row() for e in self.store.recent_events(limit=limit)]

    def plugin_events(
        self,
        plugin_id: str = "",
        prefix: str = "",
        target: str = "",
        limit: int = 100,
    ) -> List[PluginEventDict]:
        return [
            {
                "timestamp": e.timestamp.isoformat(sep=" ", timespec="seconds"),
                "level": e.level,
                "message": (
                    e.message[len(prefix) :]
                    if prefix and e.message.startswith(prefix)
                    else e.message
                ),
            }
            for e in self.store.plugin_events(plugin_id, prefix, target, limit=limit)
        ]

    # ------------------------------------------------------------------
    # Log lines
    # ------------------------------------------------------------------
    def insert_log_line(
        self,
        target: str,
        source: str,
        level: str,
        message: str,
        log_time: Optional[str] = None,
    ):
        key = f"{target}\x1f{source}\x1f{log_time or ''}\x1f{message}"
        dedup_hash = hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
        record = self.store.add_log_line(target, source, level, message, dedup_hash)
        if record is None:
            # Already in the buffer — the DB's unique index would reject it too.
            return
        _writer.submit(
            lambda: (
                LogLine.insert(
                    target=record.target,
                    source=record.source,
                    level=record.level,
                    message=record.message,
                    dedup_hash=record.dedup_hash,
                    timestamp=record.timestamp,
                )
                .on_conflict_ignore()
                .execute()
            )
        )

    def log_lines(
        self, target: str, filter_prefix: str = "", limit: int = 15
    ) -> List[LogLineModelDict]:
        return [
            line.as_row()
            for line in self.store.recent_log_lines(target, filter_prefix, limit=limit)
        ]

    # ------------------------------------------------------------------
    # Retention — a disk-only concern now
    # ------------------------------------------------------------------
    # The in-memory buffers are self-limiting (a deque drops its oldest entry
    # on append), so these prunes exist purely to stop the database file
    # growing without bound. They never touch the store.

    def prune_logs(self, retention_days: int) -> int:
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            deleted = LogLine.delete().where(LogLine.timestamp < cutoff).execute()
            if deleted:
                logging.info(
                    f"Pruned {deleted} log line(s) older than {retention_days}d"
                )

        _writer.submit(_do_prune)
        return 0

    def prune_metrics(self, retention_days: int) -> int:
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
        """The newest row per collector is kept regardless of age so a restart
        can still hydrate every monitor's last known state."""
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            newest = StatusHistory.select(fn.MAX(StatusHistory.id)).group_by(
                StatusHistory.collector_id
            )
            deleted = (
                StatusHistory.delete()
                .where(
                    (StatusHistory.timestamp < cutoff)
                    & (StatusHistory.id.not_in(newest))
                )
                .execute()
            )
            if deleted:
                logging.info(
                    f"Pruned {deleted} status row(s) older than {retention_days}d"
                )

        _writer.submit(_do_prune)
        return 0

    def prune_jobs(self, retention_days: int) -> int:
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)

        def _do_prune():
            deleted = (
                Job.delete()
                .where((Job.state != "running") & (Job.started < cutoff))
                .execute()
            )
            if deleted:
                logging.info(f"Pruned {deleted} job(s) older than {retention_days}d")

        _writer.submit(_do_prune)
        return 0

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    # Job ids are assigned by the store, so create_job returns immediately
    # instead of waiting on SQLite's autoincrement. Persistence mirrors the
    # store-assigned id into the row's primary key, keeping the two in step
    # across a restart.

    def create_job(
        self,
        plugin_id: str,
        target: str,
        kind: str,
        command: str,
        workdir: Optional[str] = None,
    ) -> int:
        record = self.store.create_job(plugin_id, target, kind, command, workdir)
        _writer.submit(
            lambda: Job.insert(
                id=record.id,
                plugin_id=record.plugin_id,
                target=record.target,
                kind=record.kind,
                command=record.command,
                state=record.state,
                started=record.started,
                workdir=record.workdir,
            )
            .on_conflict_replace()
            .execute()
        )
        return record.id

    def _update_job(self, job_id: int, **fields: Any) -> None:
        if self.store.update_job(job_id, **fields) is None:
            return
        _writer.submit(lambda: Job.update(**fields).where(Job.id == job_id).execute())

    def set_job_pid(self, job_id: int, pid: int) -> None:
        self._update_job(job_id, pid=pid)

    def bump_job_output_seq(self, job_id: int, new_seq: int) -> None:
        """How far a poll has consumed the target's output file, so the next
        poll only appends newly-arrived bytes/lines."""
        self._update_job(job_id, output_seq=new_seq)

    def set_job_progress(self, job_id: int, progress: str) -> None:
        self._update_job(job_id, progress=progress)

    def finish_job(
        self,
        job_id: int,
        state: str,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        self._update_job(
            job_id,
            state=state,
            exit_code=exit_code,
            error=error,
            finished=datetime.now(),
        )

    def append_job_output(self, job_id: int, lines, stream: str = "stdout") -> None:
        records = self.store.append_job_output(job_id, lines, stream)
        if not records:
            return
        _writer.submit(
            lambda: JobOutput.insert_many(
                [
                    {
                        "job": job_id,
                        "seq": r.seq,
                        "stream": r.stream,
                        "message": r.message,
                        "timestamp": r.timestamp,
                    }
                    for r in records
                ]
            )
            .on_conflict_ignore()
            .execute()
        )

    def get_job(self, job_id: int) -> Optional[JobDict]:
        return self.store.get_job(job_id)

    def recent_jobs(
        self,
        plugin_id: Optional[str] = None,
        limit: int = 20,
        kind: Optional[str] = None,
    ) -> List[JobDict]:
        return self.store.recent_jobs(plugin_id=plugin_id, limit=limit, kind=kind)

    def running_jobs(self, plugin_id: Optional[str] = None) -> List[JobDict]:
        return self.store.running_jobs(plugin_id=plugin_id)

    def running_jobs_with_pid(self, plugin_id: Optional[str] = None) -> List[JobDict]:
        """Running jobs launched on a target (they have a pid), for a restarted
        engine to re-adopt via polling."""
        return self.store.running_jobs(plugin_id=plugin_id, with_pid=True)

    def job_output(
        self, job_id: int, after_seq: int = -1, limit: int = 500
    ) -> List[JobOutputDict]:
        return [
            {
                "seq": o.seq,
                "timestamp": o.timestamp.isoformat(sep=" ", timespec="seconds"),
                "stream": o.stream,
                "message": o.message,
            }
            for o in self.store.job_output(job_id, after_seq=after_seq, limit=limit)
        ]

    def reconcile_orphaned_jobs(self) -> int:
        """Jobs run detached on the target and survive a Vigil restart, so a
        'running' job is not force-failed here — the owning plugin's next poll
        re-adopts it (pid alive → resume; pid/exit gone → finalize). Only jobs
        with no pid recorded (crashed between create_job and launch, so nothing
        is actually running remotely) are failed."""
        error = "Vigil restarted before this job started on the target"
        failed = self.store.reconcile_orphaned_jobs(error)
        if not failed:
            return 0
        finished = datetime.now()
        _writer.submit(
            lambda: Job.update(
                state="failed", finished=finished, error=error
            )
            .where(Job.id.in_(failed))
            .execute()
        )
        return len(failed)

    # ------------------------------------------------------------------
    # Settings and snapshots
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self.store.settings.get(key, default)

    def set_setting(self, key: str, value: str):
        self.store.settings[key] = value
        _writer.submit(
            lambda: Setting.insert(key=key, value=value).on_conflict_replace().execute()
        )

    def set_snapshot(self, plugin_id: str, data: Any):
        """``data`` is the decoded object. It is stored as-is and serialised
        only on the way to disk, so readers never pay a JSON decode."""
        self.store.snapshots[plugin_id] = data
        payload = json.dumps(data)
        _writer.submit(
            lambda: PluginSnapshot.insert(
                plugin_id=plugin_id, data=payload, updated=datetime.now()
            )
            .on_conflict_replace()
            .execute()
        )

    def latest_snapshot(self, plugin_id: str, default: Any = None) -> Any:
        return self.store.snapshots.get(plugin_id, default)

    def flush(self, timeout: Optional[float] = None):
        """Wait for queued persistence to reach disk. Only tests and shutdown
        need this — the running system never waits on the writer."""
        _writer.flush(timeout)

    # ------------------------------------------------------------------
    # Plugin-scoped write surface
    # ------------------------------------------------------------------
    # A plugin's pure parse_results()/plan_action() returns a single
    # CollectResult; the engine persists it here with one apply_result() call,
    # passing the plugin's identity (which the engine holds) rather than
    # binding it into a per-plugin object. Reads come back via PluginDataView.

    def write_event(
        self,
        target: str,
        plugin_id: str,
        plugin_name: str,
        message: str,
        level: str = "INFO",
    ) -> None:
        """A plugin event, prefixed with the plugin name for the events feed."""
        self.insert_event(
            level, f"[{plugin_name}] {message}", target, source_id=plugin_id
        )

    def apply_result(
        self, target: str, plugin_id: str, plugin_name: str, result: "CollectResult"
    ) -> None:
        """Fan a CollectResult out to the per-datatype writes. The one place
        that translates the plugin-facing CollectResult contract into store
        updates (each of which mirrors itself to disk)."""
        for name, value in result.metrics.items():
            self.insert_metric(
                target, plugin_id, name, value, result.metadata.get(name)
            )
        for message, level in result.logs:
            self.write_event(target, plugin_id, plugin_name, message, level=level)
        for message, level, log_time in result.log_lines:
            self.insert_log_line(target, plugin_id, level, message, log_time)
        if result.status is not None:
            self.insert_status(plugin_id, result.status)
        if result.snapshot is not None:
            self.set_snapshot(plugin_id, result.snapshot)
        for key, value in result.settings.items():
            self.set_setting(key, value)
