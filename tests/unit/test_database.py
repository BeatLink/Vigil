from datetime import datetime, timedelta
import pytest
from vigil.core.database.database import (
    DatabaseManager, Metric, Event, Setting, StatusHistory, LogLine, PluginSnapshot, db,
)


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


class TestLogLineLogger:
    def test_log_line_via_logger(self, mgr):
        logger = mgr.get_logger("host1", "my-plugin")
        logger.log_line("a log message", level="ERROR", log_time="2024-01-01T00:00:00")
        mgr.flush()
        with db.connection_context():
            row = LogLine.select().where(LogLine.source == "my-plugin").first()
        assert row is not None
        assert row.message == "a log message"
        assert row.level == "ERROR"
        assert row.target == "host1"

    def test_logger_dedups_repeated_line(self, mgr):
        logger = mgr.get_logger("host1", "my-plugin")
        logger.log_line("dup", log_time="t1")
        logger.log_line("dup", log_time="t1")
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


class TestInternalDatabaseLogger:
    def test_get_logger_returns_scoped_logger(self, mgr):
        logger = mgr.get_logger("host1", "my-plugin")
        assert logger.target == "host1"
        assert logger.plugin_name == "my-plugin"

    def test_write_inserts_prefixed_event(self, mgr):
        logger = mgr.get_logger("host1", "test-plugin")
        logger.write("something happened", level="WARNING")
        mgr.flush()
        with db.connection_context():
            e = Event.select().where(Event.level == "WARNING").first()
        assert e is not None
        assert "[test-plugin] something happened" in e.message
        assert e.target == "host1"

    def test_metric_inserts_metric_row(self, mgr):
        logger = mgr.get_logger("host1", "test-plugin")
        logger.metric("cpu_pct", 42.5)
        mgr.flush()
        with db.connection_context():
            m = Metric.select().where(
                (Metric.collector == "test-plugin") & (Metric.metric_name == "cpu_pct")
            ).first()
        assert m is not None
        assert m.value == pytest.approx(42.5)
        assert m.target == "host1"

    def test_snapshot_round_trips_through_get_snapshot(self, mgr):
        logger = mgr.get_logger("host1", "test-plugin", "svc-list")
        rows = [{"pid": 1, "command": "init"}, {"pid": 2, "command": "sshd"}]
        logger.snapshot(rows)
        mgr.flush()
        import json
        assert json.loads(mgr.get_snapshot("svc-list")) == rows


class TestSnapshot:
    def test_get_snapshot_returns_none_when_never_written(self, mgr):
        assert mgr.get_snapshot("never-written") is None

    def test_set_snapshot_upserts_not_appends(self, mgr):
        mgr.set_snapshot("p", '["first"]')
        mgr.flush()
        mgr.set_snapshot("p", '["second"]')
        mgr.flush()
        with db.connection_context():
            count = PluginSnapshot.select().where(PluginSnapshot.plugin_id == "p").count()
        assert count == 1
        assert mgr.get_snapshot("p") == '["second"]'

    def test_snapshots_are_scoped_by_plugin_id(self, mgr):
        mgr.set_snapshot("a", '["from-a"]')
        mgr.set_snapshot("b", '["from-b"]')
        mgr.flush()
        assert mgr.get_snapshot("a") == '["from-a"]'
        assert mgr.get_snapshot("b") == '["from-b"]'
