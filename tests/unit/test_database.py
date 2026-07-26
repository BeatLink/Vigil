from datetime import datetime, timedelta
import json
import pytest
from vigil.core.database.database import (
    DatabaseManager, Metric, Event, Job, Setting, StatusHistory, LogLine,
    PluginSnapshot, db,
)
from vigil.core.connectors.types import CollectResult


@pytest.fixture
def mgr(tmp_path):
    if not db.is_closed():
        db.close()
    manager = DatabaseManager(str(tmp_path / "test.db"))
    yield manager
    if not db.is_closed():
        db.close()


class TestDatabaseManagerInit:
    def test_creates_all_tables(self, mgr):
        with db.connection_context():
            assert Metric.table_exists()
            assert Event.table_exists()
            assert Setting.table_exists()
            assert StatusHistory.table_exists()
            assert LogLine.table_exists()
            assert PluginSnapshot.table_exists()

    def test_fresh_db_has_single_composite_metric_index(self, mgr):
        with db.connection_context():
            names = [i.name for i in db.get_indexes('metric')]
        assert names.count('metric_collector_metric_name_timestamp') == 1

    def test_migration_adds_composite_index_to_existing_db(self, tmp_path):
        import sqlite3
        path = str(tmp_path / "legacy.db")
        # A pre-index metric table, as created before the composite index existed.
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE metric (id INTEGER PRIMARY KEY, timestamp DATETIME, "
                    "target VARCHAR, collector VARCHAR, metric_name VARCHAR, "
                    "value REAL, metadata TEXT)")
        con.commit()
        con.close()

        if not db.is_closed():
            db.close()
        DatabaseManager(path)  # runs _migrate()
        with db.connection_context():
            names = [i.name for i in db.get_indexes('metric')]
        if not db.is_closed():
            db.close()
        assert 'metric_collector_metric_name_timestamp' in names


class TestMetrics:
    def test_insert_and_retrieve(self, mgr):
        mgr.insert_metric("host1", "ping", "latency_ms", 12.3)
        mgr.flush()
        with db.connection_context():
            m = Metric.select().where(
                (Metric.target == "host1") & (Metric.metric_name == "latency_ms")
            ).first()
        assert m is not None
        assert m.value == pytest.approx(12.3)
        assert m.collector == "ping"

    def test_multiple_metrics_ordered_by_timestamp(self, mgr):
        mgr.insert_metric("h", "c", "cpu", 10.0)
        mgr.insert_metric("h", "c", "cpu", 20.0)
        mgr.flush()
        with db.connection_context():
            latest = Metric.select().where(
                Metric.metric_name == "cpu"
            ).order_by(Metric.timestamp.desc()).first()
        assert latest.value == pytest.approx(20.0)

    def test_metadata_field_stored(self, mgr):
        mgr.insert_metric("h", "c", "m", 1.0, metadata='{"key": "val"}')
        mgr.flush()
        with db.connection_context():
            m = Metric.select().where(Metric.metric_name == "m").first()
        assert m.metadata == '{"key": "val"}'


class TestEvents:
    def test_insert_and_retrieve(self, mgr):
        mgr.insert_event("ERROR", "disk failed", "host1")
        mgr.flush()
        with db.connection_context():
            e = Event.select().where(Event.level == "ERROR").first()
        assert e is not None
        assert "disk failed" in e.message
        assert e.target == "host1"

    def test_null_target_allowed(self, mgr):
        mgr.insert_event("INFO", "engine started")
        mgr.flush()
        with db.connection_context():
            e = Event.select().where(Event.message == "engine started").first()
        assert e is not None
        assert e.target is None


class TestStatusHistory:
    def test_insert_and_retrieve(self, mgr):
        mgr.insert_status("plugin-a", "online")
        mgr.flush()
        with db.connection_context():
            s = StatusHistory.select().where(
                StatusHistory.collector_id == "plugin-a"
            ).first()
        assert s.state == "online"

    def test_multiple_statuses_for_same_plugin(self, mgr):
        mgr.insert_status("plugin-b", "online")
        mgr.insert_status("plugin-b", "failed")
        mgr.flush()
        with db.connection_context():
            latest = StatusHistory.select().where(
                StatusHistory.collector_id == "plugin-b"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert latest.state == "failed"


class TestLatestStatuses:
    def test_empty_when_no_status(self, mgr):
        assert mgr.latest_statuses() == {}

    def test_returns_latest_per_monitor(self, mgr):
        mgr.insert_status("a", "online")
        mgr.insert_status("b", "failed")
        mgr.insert_status("a", "warning")
        mgr.flush()
        result = mgr.latest_statuses()
        assert result == {"a": "warning", "b": "failed"}

    def test_missing_monitor_absent_from_map(self, mgr):
        mgr.insert_status("a", "online")
        mgr.flush()
        result = mgr.latest_statuses()
        assert "nonexistent" not in result

    def test_single_query_shape(self, mgr):
        for i in range(20):
            mgr.insert_status(f"m{i}", "online")
            mgr.insert_status(f"m{i}", "failed")
        mgr.flush()
        result = mgr.latest_statuses()
        assert len(result) == 20
        assert all(v == "failed" for v in result.values())


class TestLogLineStorage:
    def test_creates_logline_table(self, mgr):
        with db.connection_context():
            assert LogLine.table_exists()

    def test_insert_stores_line(self, mgr):
        mgr.insert_log_line("host1", "nginx", "INFO", "started ok")
        mgr.flush()
        with db.connection_context():
            row = LogLine.select().where(LogLine.target == "host1").first()
        assert row is not None
        assert row.message == "started ok"
        assert row.source == "nginx"
        assert row.level == "INFO"

    def test_duplicate_line_not_stored_twice(self, mgr):
        mgr.insert_log_line("h", "svc", "INFO", "same line", log_time="2024-01-01T00:00:00")
        mgr.insert_log_line("h", "svc", "INFO", "same line", log_time="2024-01-01T00:00:00")
        mgr.flush()
        with db.connection_context():
            count = LogLine.select().where(LogLine.message == "same line").count()
        assert count == 1

    def test_same_text_different_time_stored_separately(self, mgr):
        mgr.insert_log_line("h", "svc", "INFO", "tick", log_time="2024-01-01T00:00:00")
        mgr.insert_log_line("h", "svc", "INFO", "tick", log_time="2024-01-01T00:00:01")
        mgr.flush()
        with db.connection_context():
            count = LogLine.select().where(LogLine.message == "tick").count()
        assert count == 2

    def test_same_text_different_target_stored_separately(self, mgr):
        mgr.insert_log_line("hostA", "svc", "INFO", "boot")
        mgr.insert_log_line("hostB", "svc", "INFO", "boot")
        mgr.flush()
        with db.connection_context():
            count = LogLine.select().where(LogLine.message == "boot").count()
        assert count == 2

    def test_dedup_without_log_time_collapses_repeats(self, mgr):
        mgr.insert_log_line("h", "svc", "INFO", "repeated")
        mgr.insert_log_line("h", "svc", "INFO", "repeated")
        mgr.flush()
        with db.connection_context():
            count = LogLine.select().where(LogLine.message == "repeated").count()
        assert count == 1


class TestLogRetention:
    def _insert_aged(self, days_old: int, message: str):
        with db.connection_context():
            LogLine.create(
                timestamp=datetime.now() - timedelta(days=days_old),
                target="h", source="svc", level="INFO", message=message,
                dedup_hash=f"hash-{message}",
            )

    def test_prune_removes_old_lines(self, mgr):
        self._insert_aged(40, "old")
        self._insert_aged(1, "fresh")
        mgr.prune_logs(retention_days=30)
        mgr.flush()
        with db.connection_context():
            remaining = [r.message for r in LogLine.select()]
        assert remaining == ["fresh"]

    def test_prune_zero_disables_and_keeps_all(self, mgr):
        self._insert_aged(400, "ancient")
        mgr.prune_logs(retention_days=0)
        mgr.flush()
        with db.connection_context():
            assert LogLine.select().count() == 1

    def test_prune_negative_disables(self, mgr):
        self._insert_aged(400, "ancient")
        mgr.prune_logs(retention_days=-1)
        mgr.flush()
        with db.connection_context():
            assert LogLine.select().count() == 1

    def test_prune_keeps_lines_within_window(self, mgr):
        self._insert_aged(5, "recent")
        mgr.prune_logs(retention_days=30)
        mgr.flush()
        with db.connection_context():
            assert LogLine.select().count() == 1


class TestMetricRetention:
    def _insert_aged(self, days_old: int, name: str, value: float = 1.0):
        with db.connection_context():
            Metric.create(
                timestamp=datetime.now() - timedelta(days=days_old),
                target="h", collector="c", metric_name=name, value=value,
            )

    def test_prune_removes_old_metrics(self, mgr):
        self._insert_aged(40, "old")
        self._insert_aged(1, "fresh")
        mgr.prune_metrics(retention_days=30)
        mgr.flush()
        with db.connection_context():
            remaining = [m.metric_name for m in Metric.select()]
        assert remaining == ["fresh"]

    def test_prune_keeps_metrics_within_window(self, mgr):
        self._insert_aged(5, "recent")
        mgr.prune_metrics(retention_days=30)
        mgr.flush()
        with db.connection_context():
            assert Metric.select().count() == 1

    def test_prune_zero_disables_and_keeps_all(self, mgr):
        self._insert_aged(400, "ancient")
        mgr.prune_metrics(retention_days=0)
        mgr.flush()
        with db.connection_context():
            assert Metric.select().count() == 1

    def test_prune_negative_disables(self, mgr):
        self._insert_aged(400, "ancient")
        mgr.prune_metrics(retention_days=-1)
        mgr.flush()
        with db.connection_context():
            assert Metric.select().count() == 1


class TestStatusRetention:
    def _insert_aged(self, days_old: int, collector_id: str, state: str = 'online'):
        with db.connection_context():
            StatusHistory.create(
                timestamp=datetime.now() - timedelta(days=days_old),
                collector_id=collector_id, state=state,
            )

    def test_prune_removes_old_status_rows(self, mgr):
        self._insert_aged(40, "a", "online")   # old, not the newest for 'a'
        self._insert_aged(1, "a", "failed")    # newest for 'a', kept anyway
        mgr.prune_status(retention_days=30)
        mgr.flush()
        with db.connection_context():
            remaining = [s.state for s in StatusHistory.select()]
        assert remaining == ["failed"]

    def test_prune_keeps_newest_row_per_collector_even_if_old(self, mgr):
        # A plugin whose only status row is older than the window must not lose
        # its current state — latest_status* relies on it always being present.
        self._insert_aged(400, "stale", "offline")
        mgr.prune_status(retention_days=30)
        mgr.flush()
        with db.connection_context():
            rows = [(s.collector_id, s.state) for s in StatusHistory.select()]
        assert rows == [("stale", "offline")]

    def test_prune_zero_disables_and_keeps_all(self, mgr):
        self._insert_aged(400, "a", "online")
        self._insert_aged(300, "a", "failed")
        mgr.prune_status(retention_days=0)
        mgr.flush()
        with db.connection_context():
            assert StatusHistory.select().count() == 2


class TestLogLineWrites:
    def test_apply_result_writes_log_line(self, mgr):
        mgr.apply_result("host1", "my-plugin", "my-plugin", CollectResult(
            log_lines=[("a log message", "ERROR", "2024-01-01T00:00:00")]))
        mgr.flush()
        with db.connection_context():
            row = LogLine.select().where(LogLine.source == "my-plugin").first()
        assert row is not None
        assert row.message == "a log message"
        assert row.level == "ERROR"
        assert row.target == "host1"

    def test_dedups_repeated_line(self, mgr):
        mgr.insert_log_line("host1", "my-plugin", "INFO", "dup", "t1")
        mgr.insert_log_line("host1", "my-plugin", "INFO", "dup", "t1")
        mgr.flush()
        with db.connection_context():
            assert LogLine.select().where(LogLine.message == "dup").count() == 1


class TestSettings:
    def test_missing_key_returns_default(self, mgr):
        assert mgr.get_setting("nonexistent", default="fallback") == "fallback"

    def test_missing_key_returns_none_by_default(self, mgr):
        assert mgr.get_setting("nonexistent") is None

    def test_set_and_get(self, mgr):
        mgr.set_setting("theme", "dark")
        mgr.flush()
        assert mgr.get_setting("theme") == "dark"

    def test_overwrite_existing_setting(self, mgr):
        mgr.set_setting("k", "v1")
        mgr.set_setting("k", "v2")
        mgr.flush()
        assert mgr.get_setting("k") == "v2"


class TestApplyResult:
    def test_write_event_prefixes_with_plugin_name(self, mgr):
        mgr.write_event("host1", "test-plugin", "test-plugin",
                        "something happened", level="WARNING")
        mgr.flush()
        with db.connection_context():
            e = Event.select().where(Event.level == "WARNING").first()
        assert e is not None
        assert "[test-plugin] something happened" in e.message
        assert e.target == "host1"

    def test_apply_result_writes_metric(self, mgr):
        mgr.apply_result("host1", "test-plugin", "test-plugin",
                         CollectResult(metrics={"cpu_pct": 42.5}))
        mgr.flush()
        with db.connection_context():
            m = Metric.select().where(
                (Metric.collector == "test-plugin") & (Metric.metric_name == "cpu_pct")
            ).first()
        assert m is not None
        assert m.value == pytest.approx(42.5)
        assert m.target == "host1"

    def test_apply_result_fans_out_status_log_and_prefixed_event(self, mgr):
        mgr.apply_result("host1", "p", "My Plugin", CollectResult(
            metrics={"v": 1.0}, logs=[("hi", "INFO")], status="online"))
        mgr.flush()
        with db.connection_context():
            assert Metric.select().where(Metric.collector == "p").count() == 1
            assert StatusHistory.select().where(
                (StatusHistory.collector_id == "p") & (StatusHistory.state == "online")
            ).count() == 1
            e = Event.select().where(Event.source_id == "p").first()
        assert "[My Plugin] hi" in e.message

    def test_apply_result_writes_snapshot(self, mgr):
        rows = [{"pid": 1, "command": "init"}, {"pid": 2, "command": "sshd"}]
        mgr.apply_result("host1", "svc-list", "svc-list",
                         CollectResult(snapshot=rows))
        mgr.flush()
        assert mgr.latest_snapshot("svc-list") == rows

    def test_latest_snapshot_default_when_missing(self, mgr):
        assert mgr.latest_snapshot("never", default=[]) == []
        assert mgr.latest_snapshot("never") is None


class TestSnapshot:
    """Snapshots are held as decoded objects in the state store and serialised
    only on the way to disk, so these assert object round-tripping rather than
    the JSON-string handling the old query-backed reader did."""

    def test_latest_snapshot_returns_none_when_never_written(self, mgr):
        assert mgr.latest_snapshot("never-written") is None

    def test_set_snapshot_upserts_not_appends(self, mgr):
        mgr.set_snapshot("p", ["first"])
        mgr.flush()
        mgr.set_snapshot("p", ["second"])
        mgr.flush()
        with db.connection_context():
            count = PluginSnapshot.select().where(PluginSnapshot.plugin_id == "p").count()
        assert count == 1
        assert mgr.latest_snapshot("p") == ["second"]

    def test_snapshots_are_scoped_by_plugin_id(self, mgr):
        mgr.set_snapshot("a", ["from-a"])
        mgr.set_snapshot("b", ["from-b"])
        mgr.flush()
        assert mgr.latest_snapshot("a") == ["from-a"]
        assert mgr.latest_snapshot("b") == ["from-b"]

    def test_snapshot_persists_as_json_and_reloads(self, mgr):
        """The store holds the object; SQLite holds its JSON. A fresh manager
        over the same file must hydrate back to the original object."""
        rows = [{"pid": 1, "name": "sshd"}]
        mgr.set_snapshot("probe", rows)
        mgr.flush()
        with db.connection_context():
            stored = PluginSnapshot.get(PluginSnapshot.plugin_id == "probe").data
        assert json.loads(stored) == rows

        reloaded = DatabaseManager(mgr.db_path)
        assert reloaded.latest_snapshot("probe") == rows


class TestHydration:
    """SQLite's only read path. A restart must reconstruct the store from
    disk, since the store is the system of record while running."""

    def _restart(self, mgr):
        mgr.flush()
        db.close()
        return DatabaseManager(mgr.db_path)

    def test_metrics_and_history_survive_restart(self, mgr):
        for value in (1.0, 2.0, 3.0):
            mgr.insert_metric("h", "cpu", "usage", value)
        m2 = self._restart(mgr)
        assert m2.latest_metric("cpu", "usage").value == 3.0
        assert [m.value for m in m2.metric_history("cpu", "usage")] == [1.0, 2.0, 3.0]

    def test_only_the_newest_status_per_collector_is_restored(self, mgr):
        mgr.insert_status("cpu", "online")
        mgr.insert_status("cpu", "failed")
        mgr.insert_status("disk", "online")
        m2 = self._restart(mgr)
        assert m2.latest_statuses() == {"cpu": "failed", "disk": "online"}

    def test_events_survive_restart_newest_first(self, mgr):
        for i in range(3):
            mgr.insert_event("INFO", f"m{i}")
        m2 = self._restart(mgr)
        assert [e["message"] for e in m2.recent_events()] == ["m2", "m1", "m0"]

    def test_settings_survive_restart(self, mgr):
        mgr.set_setting("k", "v")
        assert self._restart(mgr).get_setting("k") == "v"

    def test_log_lines_survive_restart_with_dedup_intact(self, mgr):
        mgr.insert_log_line("h", "nginx", "INFO", "line", log_time="t1")
        m2 = self._restart(mgr)
        assert len(m2.log_lines("h", limit=0)) == 1
        # The restored dedup state must still suppress a re-collected line.
        m2.insert_log_line("h", "nginx", "INFO", "line", log_time="t1")
        assert len(m2.log_lines("h", limit=0)) == 1

    def test_running_job_survives_restart(self, mgr):
        job_id = mgr.create_job("p", "h", "backup", "cmd", workdir="/tmp/x")
        mgr.set_job_pid(job_id, 4242)
        mgr.append_job_output(job_id, ["a", "b"])
        m2 = self._restart(mgr)
        restored = m2.get_job(job_id)
        assert restored["state"] == "running"
        assert restored["pid"] == 4242
        assert [o["message"] for o in m2.job_output(job_id)] == ["a", "b"]

    def test_new_job_ids_do_not_collide_with_restored_ones(self, mgr):
        first = mgr.create_job("p", "h", "k", "cmd")
        m2 = self._restart(mgr)
        assert m2.create_job("p", "h", "k", "cmd") > first

    def test_hydration_respects_buffer_limits(self, mgr):
        from vigil.core.state import BufferSizes
        for value in range(50):
            mgr.insert_metric("h", "cpu", "usage", float(value))
        mgr.flush()
        db.close()
        m2 = DatabaseManager(mgr.db_path, buffers=BufferSizes(metric_history=5))
        history = m2.metric_history("cpu", "usage", limit=0)
        assert len(history) == 5
        # The newest points are the ones kept, and still oldest-to-newest.
        assert [m.value for m in history] == [45.0, 46.0, 47.0, 48.0, 49.0]


class TestReconcileOrphanedJobs:
    def test_pidless_job_is_failed_and_persisted(self, mgr):
        job_id = mgr.create_job("p", "h", "k", "cmd")
        assert mgr.reconcile_orphaned_jobs() == 1
        assert mgr.get_job(job_id)["state"] == "failed"
        mgr.flush()
        with db.connection_context():
            assert Job.get(Job.id == job_id).state == "failed"

    def test_job_with_pid_is_left_running(self, mgr):
        job_id = mgr.create_job("p", "h", "k", "cmd")
        mgr.set_job_pid(job_id, 99)
        assert mgr.reconcile_orphaned_jobs() == 0
        assert mgr.get_job(job_id)["state"] == "running"
