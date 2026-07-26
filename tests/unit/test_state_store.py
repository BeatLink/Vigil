"""Tests for the in-memory state store.

The store is now the system of record, so these cover the behaviour that used
to be SQLite's job: bounding growth, log dedup, ordering, and staying correct
under concurrent collector/UI access.
"""

import threading
from datetime import datetime, timedelta

import pytest

from vigil.core.state import BufferSizes, StateStore
from vigil.core.state.records import LogLineRecord, MetricRecord


@pytest.fixture
def store():
    return StateStore()


class TestDirectMutation:
    """The flat current-state maps are the read and write API."""

    def test_settings_are_a_plain_dict(self, store):
        store.settings["k"] = "v"
        assert store.settings["k"] == "v"
        assert store.settings.get("missing", "d") == "d"

    def test_snapshots_hold_decoded_objects(self, store):
        rows = [{"pid": 1}]
        store.snapshots["p"] = rows
        # The same object comes back — no serialise/parse round trip.
        assert store.snapshots["p"] is rows


class TestMetrics:
    def test_latest_metric_is_the_newest_append(self, store):
        for value in (1.0, 2.0, 3.0):
            store.add_metric("h", "cpu", "usage", value)
        assert store.latest_metric("cpu", "usage").value == 3.0

    def test_latest_metric_none_when_unseen(self, store):
        assert store.latest_metric("nope", "usage") is None

    def test_history_is_oldest_to_newest(self, store):
        for value in (1.0, 2.0, 3.0):
            store.add_metric("h", "cpu", "usage", value)
        assert [m.value for m in store.metric_history("cpu", "usage")] == [1.0, 2.0, 3.0]

    def test_history_returns_the_newest_within_limit(self, store):
        for value in range(10):
            store.add_metric("h", "cpu", "usage", float(value))
        assert [m.value for m in store.metric_history("cpu", "usage", limit=3)] == [
            7.0,
            8.0,
            9.0,
        ]

    def test_series_is_bounded_by_buffer_size(self):
        store = StateStore(BufferSizes(metric_history=5))
        for value in range(100):
            store.add_metric("h", "cpu", "usage", float(value))
        history = store.metric_history("cpu", "usage", limit=0)
        assert len(history) == 5
        assert [m.value for m in history] == [95.0, 96.0, 97.0, 98.0, 99.0]

    def test_series_are_kept_separate(self, store):
        store.add_metric("h", "cpu", "usage", 1.0)
        store.add_metric("h", "cpu", "temp", 2.0)
        store.add_metric("h", "disk", "usage", 3.0)
        assert store.latest_metric("cpu", "usage").value == 1.0
        assert store.latest_metric("cpu", "temp").value == 2.0
        assert store.latest_metric("disk", "usage").value == 3.0

    def test_collector_metrics_spans_series_and_excludes_others(self, store):
        store.add_metric("h", "cpu", "usage", 1.0)
        store.add_metric("h", "cpu", "temp", 2.0)
        store.add_metric("h", "disk", "usage", 3.0)
        records = store.collector_metrics("cpu", limit=10)
        assert {m.collector for m in records} == {"cpu"}
        assert {m.metric_name for m in records} == {"usage", "temp"}

    def test_collector_metrics_is_newest_first(self, store):
        for value in range(5):
            store.add_metric("h", "cpu", "usage", float(value))
        records = store.collector_metrics("cpu", limit=3)
        assert [m.value for m in records] == [4.0, 3.0, 2.0]

    def test_latest_metrics_takes_one_point_per_series(self, store):
        for value in range(5):
            store.add_metric("h", "cpu", "usage", float(value))
            store.add_metric("h", "cpu", "temp", float(value))
        latest = store.latest_metrics()
        assert len(latest) == 2
        assert {m.value for m in latest} == {4.0}


class TestStatus:
    def test_status_is_last_write_wins(self, store):
        store.statuses["cpu"] = _status("cpu", "online")
        store.statuses["cpu"] = _status("cpu", "failed")
        assert store.statuses["cpu"].state == "failed"

    def test_status_does_not_accumulate_history(self, store):
        for _ in range(100):
            store.statuses["cpu"] = _status("cpu", "online")
        assert len(store.statuses) == 1


class TestEvents:
    def test_recent_events_is_newest_first(self, store):
        for i in range(3):
            store.add_event("INFO", f"m{i}")
        assert [e.message for e in store.recent_events()] == ["m2", "m1", "m0"]

    def test_events_are_bounded(self):
        store = StateStore(BufferSizes(event_history=10))
        for i in range(100):
            store.add_event("INFO", f"m{i}")
        events = store.recent_events(limit=0)
        assert len(events) == 10
        assert events[0].message == "m99"

    def test_filters_compose(self, store):
        store.add_event("INFO", "keep me", target="h1")
        store.add_event("ERROR", "keep me", target="h1")
        store.add_event("ERROR", "keep me", target="h2")
        store.add_event("ERROR", "other", target="h1")
        results = store.recent_events(level="ERROR", target="h1", search="keep")
        assert len(results) == 1

    def test_limit_applies_after_filtering(self, store):
        for i in range(50):
            store.add_event("ERROR" if i % 2 else "INFO", f"m{i}")
        assert len(store.recent_events(limit=5, level="ERROR")) == 5

    def test_plugin_events_prefers_source_id(self, store):
        store.add_event("INFO", "[A] mine", source_id="a")
        store.add_event("INFO", "[A] theirs", source_id="b")
        results = store.plugin_events(plugin_id="a")
        assert [e.message for e in results] == ["[A] mine"]

    def test_plugin_events_falls_back_to_prefix(self, store):
        store.add_event("INFO", "[A] mine", target="h")
        store.add_event("INFO", "[B] theirs", target="h")
        results = store.plugin_events(prefix="[A] ", target="h")
        assert [e.message for e in results] == ["[A] mine"]


class TestLogLines:
    def test_duplicate_lines_are_rejected(self, store):
        assert store.add_log_line("h", "s", "INFO", "line", "hash1") is not None
        assert store.add_log_line("h", "s", "INFO", "line", "hash1") is None
        assert len(store.recent_log_lines("h", limit=0)) == 1

    def test_distinct_lines_are_kept(self, store):
        store.add_log_line("h", "s", "INFO", "a", "hash1")
        store.add_log_line("h", "s", "INFO", "b", "hash2")
        assert len(store.recent_log_lines("h", limit=0)) == 2

    def test_dedup_set_does_not_grow_past_the_buffer(self):
        """A hash that has aged out of the buffer must age out of the dedup
        set too, or the set is an unbounded leak."""
        store = StateStore(BufferSizes(log_history=5))
        for i in range(100):
            store.add_log_line("h", "s", "INFO", f"line{i}", f"hash{i}")
        assert len(store._log_dedup["h"]) <= 5
        # An evicted line is no longer a known duplicate, so it re-enters.
        assert store.add_log_line("h", "s", "INFO", "line0", "hash0") is not None

    def test_targets_are_isolated(self, store):
        store.add_log_line("h1", "s", "INFO", "a", "hash1")
        store.add_log_line("h2", "s", "INFO", "b", "hash2")
        assert len(store.recent_log_lines("h1", limit=0)) == 1
        assert len(store.recent_log_lines("h2", limit=0)) == 1

    def test_source_filter(self, store):
        store.add_log_line("h", "nginx", "INFO", "a", "hash1")
        store.add_log_line("h", "sshd", "INFO", "b", "hash2")
        results = store.recent_log_lines("h", filter_prefix="nginx", limit=0)
        assert [r.message for r in results] == ["a"]

    def test_newest_first(self, store):
        for i in range(3):
            store.add_log_line("h", "s", "INFO", f"line{i}", f"hash{i}")
        assert [r.message for r in store.recent_log_lines("h")] == [
            "line2",
            "line1",
            "line0",
        ]


class TestJobs:
    def test_ids_are_unique_and_ascending(self, store):
        ids = [store.create_job("p", "h", "k", "cmd").id for _ in range(5)]
        assert ids == [1, 2, 3, 4, 5]

    def test_update_advances_in_place(self, store):
        job = store.create_job("p", "h", "k", "cmd")
        store.update_job(job.id, pid=42, progress="50%")
        assert store.get_job(job.id)["pid"] == 42
        assert store.get_job(job.id)["progress"] == "50%"

    def test_update_of_unknown_job_is_a_noop(self, store):
        assert store.update_job(999, pid=1) is None

    def test_running_filters(self, store):
        a = store.create_job("p1", "h", "k", "cmd")
        b = store.create_job("p2", "h", "k", "cmd")
        store.update_job(b.id, pid=7)
        assert len(store.running_jobs()) == 2
        assert len(store.running_jobs(plugin_id="p1")) == 1
        assert len(store.running_jobs(with_pid=True)) == 1

    def test_finished_job_is_not_running(self, store):
        job = store.create_job("p", "h", "k", "cmd")
        store.update_job(job.id, state="success", finished=datetime.now())
        assert store.running_jobs() == []
        assert store.get_job(job.id)["running"] is False

    def test_output_sequence_is_contiguous_across_appends(self, store):
        job = store.create_job("p", "h", "k", "cmd")
        store.append_job_output(job.id, ["a", "b"])
        store.append_job_output(job.id, ["c"])
        assert [o.seq for o in store.job_output(job.id)] == [0, 1, 2]

    def test_output_after_seq_returns_only_newer(self, store):
        job = store.create_job("p", "h", "k", "cmd")
        store.append_job_output(job.id, ["a", "b", "c"])
        assert [o.message for o in store.job_output(job.id, after_seq=0)] == ["b", "c"]

    def test_output_skips_none_lines(self, store):
        job = store.create_job("p", "h", "k", "cmd")
        store.append_job_output(job.id, ["a", None, "b"])
        assert [o.message for o in store.job_output(job.id)] == ["a", "b"]

    def test_reconcile_fails_only_pidless_jobs(self, store):
        launched = store.create_job("p", "h", "k", "cmd")
        store.update_job(launched.id, pid=99)
        stranded = store.create_job("p", "h", "k", "cmd")

        failed = store.reconcile_orphaned_jobs("restarted")

        assert failed == [stranded.id]
        # The job with a live remote pid is left for its plugin to re-adopt.
        assert store.get_job(launched.id)["state"] == "running"
        assert store.get_job(stranded.id)["state"] == "failed"

    def test_finished_jobs_are_evicted_past_the_cap(self):
        """Jobs own an output buffer each, so unlike the deque-backed streams
        they need explicit eviction or a host running frequent backups
        accumulates them for the life of the process."""
        store = StateStore(BufferSizes(jobs_per_plugin=3))
        ids = []
        for _ in range(10):
            job = store.create_job("p", "h", "k", "cmd")
            store.append_job_output(job.id, ["out"])
            store.update_job(job.id, state="success")
            ids.append(job.id)

        assert len(store.jobs) == 3
        # The newest three survive; the rest are gone with their output.
        assert sorted(store.jobs) == ids[-3:]
        assert store.get_job(ids[0]) is None
        assert store.job_output(ids[0]) == []
        assert set(store.job_output_buffers) == set(ids[-3:])

    def test_running_job_keeps_its_full_output(self):
        """A plugin tails its own running job to parse progress, so the live
        buffer must not be trimmed underneath it."""
        store = StateStore(BufferSizes(finished_job_output=10))
        job = store.create_job("p", "h", "k", "cmd")
        store.append_job_output(job.id, [f"line{i}" for i in range(100)])
        assert len(store.job_output(job.id, limit=0)) == 100

    def test_finished_job_output_is_trimmed_to_the_tail(self):
        """Once a job ends its output is only read on demand (from disk), so
        only a short tail stays resident — otherwise (monitors x jobs x
        output) lines pile up to serve a view almost nobody opens."""
        store = StateStore(BufferSizes(finished_job_output=10))
        job = store.create_job("p", "h", "k", "cmd")
        store.append_job_output(job.id, [f"line{i}" for i in range(100)])
        store.update_job(job.id, state="success")

        remaining = store.job_output(job.id, limit=0)
        assert len(remaining) == 10
        # The tail is kept, and sequence numbers stay intact for after_seq.
        assert [r.message for r in remaining] == [f"line{i}" for i in range(90, 100)]
        assert [r.seq for r in remaining] == list(range(90, 100))

    def test_short_finished_job_output_is_untouched(self):
        store = StateStore(BufferSizes(finished_job_output=10))
        job = store.create_job("p", "h", "k", "cmd")
        store.append_job_output(job.id, ["a", "b"])
        store.update_job(job.id, state="success")
        assert [r.message for r in store.job_output(job.id)] == ["a", "b"]

    def test_running_jobs_are_never_evicted(self):
        store = StateStore(BufferSizes(jobs_per_plugin=2))
        running = store.create_job("p", "h", "k", "cmd")
        for _ in range(10):
            job = store.create_job("p", "h", "k", "cmd")
            store.update_job(job.id, state="success")
        assert store.get_job(running.id)["state"] == "running"

    def test_eviction_is_per_plugin(self):
        store = StateStore(BufferSizes(jobs_per_plugin=2))
        for plugin in ("a", "b"):
            for _ in range(5):
                job = store.create_job(plugin, "h", "k", "cmd")
                store.update_job(job.id, state="success")
        assert len(store.recent_jobs(plugin_id="a", limit=99)) == 2
        assert len(store.recent_jobs(plugin_id="b", limit=99)) == 2

    def test_hydration_respects_the_job_cap(self):
        from vigil.core.state.records import JobRecord

        store = StateStore(BufferSizes(jobs_per_plugin=2))
        store.load_jobs(
            [
                JobRecord(
                    id=i,
                    plugin_id="p",
                    target="h",
                    kind="k",
                    state="success",
                    command="cmd",
                    started=datetime.now() + timedelta(seconds=i),
                )
                for i in range(1, 11)
            ],
            {},
        )
        assert len(store.jobs) == 2
        # Ids still resume past the highest restored id, evicted or not.
        assert store.create_job("p", "h", "k", "cmd").id == 11

    def test_restored_ids_do_not_collide(self, store):
        from vigil.core.state.records import JobRecord

        store.load_jobs(
            [
                JobRecord(
                    id=7,
                    plugin_id="p",
                    target="h",
                    kind="k",
                    state="success",
                    command="cmd",
                    started=datetime.now(),
                )
            ],
            {},
        )
        assert store.create_job("p", "h", "k", "cmd").id == 8


class TestConcurrency:
    def test_concurrent_metric_writes_lose_nothing(self, store):
        """Collectors run on worker threads while the UI reads; appends from
        several threads must all land."""

        def write(worker):
            for i in range(200):
                store.add_metric("h", f"c{worker}", "m", float(i))

        threads = [threading.Thread(target=write, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for worker in range(8):
            assert len(store.metric_history(f"c{worker}", "m", limit=0)) == 200

    def test_reads_are_safe_while_writes_are_in_flight(self, store):
        """A reader iterating a buffer while a collector appends must not raise
        (which a bare deque iteration would)."""
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            while not stop.is_set():
                store.add_metric("h", "cpu", "m", float(i))
                store.add_event("INFO", f"e{i}")
                store.add_log_line("h", "s", "INFO", f"l{i}", f"hash{i}")
                i += 1

        def reader():
            try:
                while not stop.is_set():
                    store.metric_history("cpu", "m", limit=30)
                    store.collector_metrics("cpu", limit=15)
                    store.recent_events(limit=200)
                    store.recent_log_lines("h", limit=15)
                    store.latest_metrics()
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        threading.Event().wait(0.3)
        stop.set()
        for t in threads:
            t.join()

        assert errors == []

    def test_concurrent_job_creation_yields_unique_ids(self, store):
        ids = []
        lock = threading.Lock()

        def create():
            for _ in range(50):
                job = store.create_job("p", "h", "k", "cmd")
                with lock:
                    ids.append(job.id)

        threads = [threading.Thread(target=create) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(ids) == len(set(ids)) == 400


def _status(collector_id, state):
    from vigil.core.state.records import StatusRecord

    return StatusRecord(
        collector_id=collector_id, state=state, timestamp=datetime.now()
    )
