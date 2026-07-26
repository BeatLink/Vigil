"""StateStore — the in-memory system of record.

Every read the UI performs is served from here, from live Python objects.
Collectors write here first; persistence to SQLite happens afterwards and
asynchronously, entirely off the read path. Nothing reads back from SQLite
except ``hydrate()`` at startup.

Shape of the state
------------------
Two kinds of data live here, and they are bounded differently:

*Current state* — statuses, snapshots, settings, jobs — is bounded by the
number of configured monitors, so it is held whole.

*History* — metrics, events, log lines, job output — is unbounded over time,
so each stream is a ``deque`` with a ``maxlen``. The buffers are sized well
above what the UI asks for (charts read 30 points, log tables 15 rows, the
events feed 200), so every live read is served from memory; SQLite retains
the longer tail for whatever outlives the buffer.

Access
------
State is public and mutated directly. ``statuses``, ``settings`` and
``snapshots`` are plain dicts of immutable records — read and write them as
dicts (``store.settings[key] = value``), with no accessor in between; that
directness is the point of moving off the database.

Methods exist only where there is logic beyond assignment: appending to the
right bounded buffer, creating a series on first use, log dedup, ordering and
slicing history. Those aren't accessors — they are what keeps the containers
correct, and inlining them would push that burden onto every collector.

Concurrency
-----------
Collectors run on asyncio worker threads (``asyncio.to_thread``) while the UI
reads from NiceGUI's loop and its own offload pool, so the store is touched
from several threads. One ``threading.RLock`` guards the compound operations
— the ones that read-modify-write a buffer, where an interleaved collector
could otherwise corrupt the sequence or lose an entry. It is reentrant
because a few writes compose.

The directly-mutated dicts need no lock: they hold immutable records, and a
single dict get/set is atomic under the GIL, so a reader either sees the old
record or the new one and never a half-written state.

Readers of the *buffers* copy out under the lock and return lists of
immutable records rather than the live deques — a caller iterating a deque
while a collector appends to it would otherwise risk a mutation error. The
copies are bounded by the caller's ``limit`` and cost far less than the
SQLite round-trip plus cache layer they replace.
"""

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from vigil.core.state.records import (
    EventRecord,
    JobOutputRecord,
    JobRecord,
    LogLineRecord,
    MetricRecord,
    StatusRecord,
)


@dataclass(frozen=True)
class BufferSizes:
    """How much history each in-memory stream retains. Defaults comfortably
    exceed every limit the UI actually requests; see ``memory:`` in
    config.yaml to tune them."""

    metric_history: int = 300
    event_history: int = 2000
    log_history: int = 500
    job_output: int = 5000
    # Finished jobs kept per plugin. Unlike the other streams a job is not a
    # fixed-size deque entry — it owns an output buffer — so completed jobs are
    # evicted oldest-first once a plugin has this many, or a host running
    # frequent backups would accumulate them for the life of the process.
    # Running jobs are never evicted. The job panel reads ~20, and total
    # residency is (monitors x this), so it stays modest by default.
    jobs_per_plugin: int = 25
    # Output lines kept for a *finished* job. A running job keeps the full
    # `job_output` buffer because the UI streams it live. Once it ends, the
    # output is only read if a user opens that particular job — rare, and
    # served from disk — so only a short tail is kept resident. Without this,
    # (monitors x jobs_per_plugin x job_output) lines stay in memory to serve
    # a view almost nobody opens; that product is where job memory runs away.
    finished_job_output: int = 50


class StateStore:
    def __init__(self, buffers: Optional[BufferSizes] = None):
        self._lock = threading.RLock()
        self.buffers = buffers or BufferSizes()

        # --- current state: mutate these directly ---
        # Each holds immutable records (or plain values) keyed by id, so
        # ordinary dict access is both the read and the write API.
        self.statuses: Dict[str, StatusRecord] = {}
        self.snapshots: Dict[str, Any] = {}
        self.settings: Dict[str, str] = {}

        # --- history: bounded buffers, appended via the methods below ---
        # Metrics are keyed by (collector, metric_name) because that is how
        # every read filters them; a per-series deque means a chart read is a
        # dict lookup plus a slice, with no scan over other series.
        self.metrics: Dict[Tuple[str, str], deque] = {}
        # Secondary index: collector -> its series' deques. The metric table
        # reads every series of one collector, which would otherwise scan all
        # series of all monitors. Holds the same deque objects as `metrics`,
        # so appends are visible through both and there is nothing to sync.
        self._by_collector: Dict[str, List[deque]] = {}
        self.events: deque = deque(maxlen=self.buffers.event_history)
        self.log_lines: Dict[str, deque] = {}
        self._log_dedup: Dict[str, set] = {}

        # --- jobs ---
        # Records are mutable and advanced in place by successive polls, so
        # they are reached through the job methods rather than mutated bare.
        self.jobs: Dict[int, JobRecord] = {}
        self.job_output_buffers: Dict[int, deque] = {}
        self._next_job_id = 1

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------
    def add_metric(
        self,
        target: str,
        collector: str,
        metric_name: str,
        value: float,
        metadata: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> MetricRecord:
        record = MetricRecord(
            target=target,
            collector=collector,
            metric_name=metric_name,
            value=value,
            metadata=metadata,
            timestamp=timestamp or datetime.now(),
        )
        with self._lock:
            series = self._series(collector, metric_name)
            series.append(record)
        return record

    def _series(self, collector: str, metric_name: str) -> deque:
        """The deque for one (collector, metric_name), created on first use and
        registered in the by-collector index. Callers hold the lock."""
        key = (collector, metric_name)
        series = self.metrics.get(key)
        if series is None:
            series = self.metrics[key] = deque(maxlen=self.buffers.metric_history)
            self._by_collector.setdefault(collector, []).append(series)
        return series

    def latest_metric(self, collector: str, metric_name: str) -> Optional[MetricRecord]:
        series = self.metrics.get((collector, metric_name))
        return series[-1] if series else None

    def metric_history(
        self, collector: str, metric_name: str, limit: int = 30
    ) -> List[MetricRecord]:
        """Oldest-to-newest, matching what the charts plot."""
        with self._lock:
            series = self.metrics.get((collector, metric_name))
            if not series:
                return []
            return list(series)[-limit:] if limit else list(series)

    def collector_metrics(self, collector: str, limit: int = 15) -> List[MetricRecord]:
        """Every series for one collector, newest first — the metric table.

        Only the newest ``limit`` entries of each series can reach the result,
        so each deque is sliced before merging rather than copied whole. That
        keeps the cost proportional to (series for this collector x limit)
        instead of to the collector's entire retained history."""
        with self._lock:
            series_list = self._by_collector.get(collector)
            if not series_list:
                return []
            records: List[MetricRecord] = []
            for series in series_list:
                records.extend(list(series)[-limit:] if limit else series)
        records.sort(key=lambda m: m.timestamp, reverse=True)
        return records[:limit] if limit else records

    def latest_metrics(self) -> List[MetricRecord]:
        """The newest point of every series — the exporters' and REST API's
        view of "current metrics"."""
        with self._lock:
            return [series[-1] for series in self.metrics.values() if series]

    def recent_metrics(self, limit: int = 20) -> List[MetricRecord]:
        """Newest metrics across all series — the global dashboard table."""
        with self._lock:
            records = [m for series in self.metrics.values() for m in series]
        records.sort(key=lambda m: m.timestamp, reverse=True)
        return records[:limit] if limit else records

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def add_event(
        self,
        level: str,
        message: str,
        target: Optional[str] = None,
        source_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> EventRecord:
        record = EventRecord(
            level=level,
            message=message,
            target=target,
            source_id=source_id,
            timestamp=timestamp or datetime.now(),
        )
        with self._lock:
            self.events.append(record)
        return record

    def recent_events(
        self,
        limit: int = 200,
        level: Optional[str] = None,
        target: Optional[str] = None,
        search: Optional[str] = None,
    ) -> List[EventRecord]:
        """Newest first, with the same optional filters the events feed offers.
        Filtering walks the buffer in reverse and stops at ``limit``, so a
        narrow filter costs no more than the rows it returns."""
        with self._lock:
            snapshot = list(self.events)

        results: List[EventRecord] = []
        for record in reversed(snapshot):
            if level and record.level != level:
                continue
            if target and record.target != target:
                continue
            if search and search not in record.message:
                continue
            results.append(record)
            if limit and len(results) >= limit:
                break
        return results

    def plugin_events(
        self,
        plugin_id: str = "",
        prefix: str = "",
        target: str = "",
        limit: int = 100,
    ) -> List[EventRecord]:
        """Events for one plugin — by source_id when known, else by the
        ``[Plugin Name] `` message prefix the events feed writes."""
        with self._lock:
            snapshot = list(self.events)

        results: List[EventRecord] = []
        for record in reversed(snapshot):
            if plugin_id:
                if record.source_id != plugin_id:
                    continue
            else:
                if not record.message.startswith(prefix):
                    continue
                if target and record.target != target:
                    continue
            results.append(record)
            if limit and len(results) >= limit:
                break
        return results

    # ------------------------------------------------------------------
    # Log lines
    # ------------------------------------------------------------------
    def add_log_line(
        self,
        target: str,
        source: str,
        level: str,
        message: str,
        dedup_hash: str,
        timestamp: Optional[datetime] = None,
    ) -> Optional[LogLineRecord]:
        """Returns None when the line is a duplicate, so the caller can skip
        persisting it too. Log collectors re-read the same tail on every poll,
        so this dedup is what keeps a 5s poll from appending the same 15 lines
        forever — it replaces the DB's ``unique(dedup_hash)`` + on_conflict."""
        record = LogLineRecord(
            target=target,
            source=source,
            level=level,
            message=message,
            dedup_hash=dedup_hash,
            timestamp=timestamp or datetime.now(),
        )
        with self._lock:
            buffer = self.log_lines.get(target)
            if buffer is None:
                buffer = self.log_lines[target] = deque(maxlen=self.buffers.log_history)
                self._log_dedup[target] = set()

            seen = self._log_dedup[target]
            if dedup_hash in seen:
                return None

            evicted = buffer[0] if len(buffer) == buffer.maxlen else None
            buffer.append(record)
            seen.add(dedup_hash)
            # The dedup set has to age out with the buffer or it grows without
            # bound; a line that has fallen out of the buffer can no longer be
            # observed as a duplicate anyway.
            if evicted is not None:
                seen.discard(evicted.dedup_hash)
        return record

    def recent_log_lines(
        self, target: str, filter_prefix: str = "", limit: int = 15
    ) -> List[LogLineRecord]:
        """Newest first. ``filter_prefix`` matches the source exactly, as the
        DB read it replaced did."""
        with self._lock:
            buffer = self.log_lines.get(target)
            snapshot = list(buffer) if buffer else []

        results = [
            record
            for record in reversed(snapshot)
            if not filter_prefix or record.source == filter_prefix
        ]
        return results[:limit] if limit else results

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def create_job(
        self,
        plugin_id: str,
        target: str,
        kind: str,
        command: str,
        workdir: Optional[str] = None,
        state: str = "running",
        started: Optional[datetime] = None,
    ) -> JobRecord:
        """Ids are assigned here rather than by SQLite's autoincrement, so a
        job has an id the instant it is created and the caller never waits on
        a write."""
        with self._lock:
            job_id = self._next_job_id
            self._next_job_id += 1
            record = JobRecord(
                id=job_id,
                plugin_id=plugin_id,
                target=target,
                kind=kind,
                state=state,
                command=command,
                started=started or datetime.now(),
                workdir=workdir,
            )
            self.jobs[job_id] = record
            return record

    def update_job(self, job_id: int, **fields: Any) -> Optional[JobRecord]:
        """Advances a job in place. Held under the lock so a reader rendering
        the record never observes a half-applied update."""
        with self._lock:
            record = self.jobs.get(job_id)
            if record is None:
                return None
            was_running = record.state == "running"
            for key, value in fields.items():
                setattr(record, key, value)
            # A job that just finished makes its plugin's history one longer,
            # which is the only moment eviction can become necessary.
            if was_running and record.state != "running":
                self._trim_finished_output(record.id)
                self._evict_finished_jobs(record.plugin_id)
            return record

    def _trim_finished_output(self, job_id: int) -> None:
        """Shrink a completed job's output to the tail the UI still shows.
        Caller holds the lock."""
        buffer = self.job_output_buffers.get(job_id)
        keep = self.buffers.finished_job_output
        if buffer is None or len(buffer) <= keep:
            return
        trimmed = deque(list(buffer)[-keep:], maxlen=keep)
        self.job_output_buffers[job_id] = trimmed

    def _evict_finished_jobs(self, plugin_id: str) -> None:
        """Drop this plugin's oldest finished jobs (and their output buffers)
        beyond ``jobs_per_plugin``. Running jobs are exempt — they are still
        being advanced by their plugin's poll. Caller holds the lock."""
        finished = [
            record
            for record in self.jobs.values()
            if record.plugin_id == plugin_id and record.state != "running"
        ]
        excess = len(finished) - self.buffers.jobs_per_plugin
        if excess <= 0:
            return
        finished.sort(key=lambda j: j.started)
        for record in finished[:excess]:
            del self.jobs[record.id]
            self.job_output_buffers.pop(record.id, None)

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self.jobs.get(job_id)
            return record.as_dict() if record else None

    def recent_jobs(
        self,
        plugin_id: Optional[str] = None,
        limit: int = 20,
        kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = [
                record
                for record in self.jobs.values()
                if (not plugin_id or record.plugin_id == plugin_id)
                and (not kind or record.kind == kind)
            ]
            records.sort(key=lambda j: j.started, reverse=True)
            return [record.as_dict() for record in records[:limit]]

    def running_jobs(
        self, plugin_id: Optional[str] = None, with_pid: bool = False
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = [
                record
                for record in self.jobs.values()
                if record.state == "running"
                and (not plugin_id or record.plugin_id == plugin_id)
                and (not with_pid or record.pid is not None)
            ]
            records.sort(key=lambda j: j.started, reverse=True)
            return [record.as_dict() for record in records]

    def append_job_output(
        self, job_id: int, lines: Iterable[str], stream: str = "stdout"
    ) -> List[JobOutputRecord]:
        lines = [line for line in lines if line is not None]
        if not lines:
            return []
        now = datetime.now()
        with self._lock:
            buffer = self.job_output_buffers.get(job_id)
            if buffer is None:
                buffer = self.job_output_buffers[job_id] = deque(
                    maxlen=self.buffers.job_output
                )
            start = buffer[-1].seq + 1 if buffer else 0
            records = [
                JobOutputRecord(
                    seq=start + i, stream=stream, message=line, timestamp=now
                )
                for i, line in enumerate(lines)
            ]
            buffer.extend(records)
        return records

    def job_output(
        self, job_id: int, after_seq: int = -1, limit: int = 500
    ) -> List[JobOutputRecord]:
        """Output for a job, newest-seq-ordered from ``after_seq``.

        A **running** job's buffer is complete, which is what the consumers
        need: a plugin tailing its own job to parse progress, and the live
        job view. A **finished** job keeps only a short tail in memory
        (``finished_job_output``) — its complete output remains on disk, so a
        future caller that needs the full log of an old job must read it from
        there rather than assume this returns everything."""
        with self._lock:
            buffer = self.job_output_buffers.get(job_id)
            if not buffer:
                return []
            results = [record for record in buffer if record.seq > after_seq]
        return results[:limit] if limit else results

    def reconcile_orphaned_jobs(self, error: str) -> List[int]:
        """A job with no pid recorded never actually launched on its target, so
        nothing survived the restart to re-adopt — fail those. Jobs that do
        have a pid are left running for the owning plugin's next poll to
        re-adopt. Returns the ids failed, for persistence."""
        failed: List[int] = []
        now = datetime.now()
        with self._lock:
            for record in self.jobs.values():
                if record.state == "running" and record.pid is None:
                    record.state = "failed"
                    record.finished = now
                    record.error = error
                    failed.append(record.id)
        return failed

    # ------------------------------------------------------------------
    # Hydration
    # ------------------------------------------------------------------
    # Called once at startup, before collectors or the UI are running. The
    # flat dicts (statuses/settings/snapshots) are loaded by the caller with
    # ordinary ``.update()``; only the bounded buffers need these, since each
    # entry has to land in the right per-key deque.

    def load_metrics(self, records: Iterable[MetricRecord]) -> None:
        """Bulk-load oldest-first metric history."""
        with self._lock:
            for record in records:
                self._series(record.collector, record.metric_name).append(record)

    def load_events(self, records: Iterable[EventRecord]) -> None:
        with self._lock:
            self.events.extend(records)

    def load_log_lines(self, records: Iterable[LogLineRecord]) -> None:
        with self._lock:
            for record in records:
                buffer = self.log_lines.get(record.target)
                if buffer is None:
                    buffer = self.log_lines[record.target] = deque(
                        maxlen=self.buffers.log_history
                    )
                    self._log_dedup[record.target] = set()
                buffer.append(record)
                self._log_dedup[record.target].add(record.dedup_hash)

    def load_jobs(
        self, jobs: Iterable[JobRecord], output: Dict[int, List[JobOutputRecord]]
    ) -> None:
        """Restores jobs and their output, and resumes id assignment past the
        highest restored id so new jobs never collide with persisted ones."""
        with self._lock:
            plugin_ids = set()
            for record in jobs:
                self.jobs[record.id] = record
                plugin_ids.add(record.plugin_id)
                # Resume id assignment past the highest restored id even if the
                # record is about to be evicted, so a new job can never reuse an
                # id that already exists on disk.
                self._next_job_id = max(self._next_job_id, record.id + 1)
            for job_id, records in output.items():
                buffer = self.job_output_buffers[job_id] = deque(
                    maxlen=self.buffers.job_output
                )
                buffer.extend(records)
            for plugin_id in plugin_ids:
                self._evict_finished_jobs(plugin_id)
