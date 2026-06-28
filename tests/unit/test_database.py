import pytest
from vigil.core.data.database import DatabaseManager, Metric, Event, Setting, StatusHistory, db


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


class TestMetrics:
    def test_insert_and_retrieve(self, mgr):
        mgr.insert_metric("host1", "ping", "latency_ms", 12.3)
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
        with db.connection_context():
            latest = Metric.select().where(
                Metric.metric_name == "cpu"
            ).order_by(Metric.timestamp.desc()).first()
        assert latest.value == pytest.approx(20.0)

    def test_metadata_field_stored(self, mgr):
        mgr.insert_metric("h", "c", "m", 1.0, metadata='{"key": "val"}')
        with db.connection_context():
            m = Metric.select().where(Metric.metric_name == "m").first()
        assert m.metadata == '{"key": "val"}'


class TestEvents:
    def test_insert_and_retrieve(self, mgr):
        mgr.insert_event("ERROR", "disk failed", "host1")
        with db.connection_context():
            e = Event.select().where(Event.level == "ERROR").first()
        assert e is not None
        assert "disk failed" in e.message
        assert e.target == "host1"

    def test_null_target_allowed(self, mgr):
        mgr.insert_event("INFO", "engine started")
        with db.connection_context():
            e = Event.select().where(Event.message == "engine started").first()
        assert e is not None
        assert e.target is None


class TestStatusHistory:
    def test_insert_and_retrieve(self, mgr):
        mgr.insert_status("plugin-a", "online")
        with db.connection_context():
            s = StatusHistory.select().where(
                StatusHistory.collector_id == "plugin-a"
            ).first()
        assert s.state == "online"

    def test_multiple_statuses_for_same_plugin(self, mgr):
        mgr.insert_status("plugin-b", "online")
        mgr.insert_status("plugin-b", "failed")
        with db.connection_context():
            latest = StatusHistory.select().where(
                StatusHistory.collector_id == "plugin-b"
            ).order_by(StatusHistory.timestamp.desc()).first()
        assert latest.state == "failed"


class TestSettings:
    def test_missing_key_returns_default(self, mgr):
        assert mgr.get_setting("nonexistent", default="fallback") == "fallback"

    def test_missing_key_returns_none_by_default(self, mgr):
        assert mgr.get_setting("nonexistent") is None

    def test_set_and_get(self, mgr):
        mgr.set_setting("theme", "dark")
        assert mgr.get_setting("theme") == "dark"

    def test_overwrite_existing_setting(self, mgr):
        mgr.set_setting("k", "v1")
        mgr.set_setting("k", "v2")
        assert mgr.get_setting("k") == "v2"


class TestInternalDatabaseLogger:
    def test_get_logger_returns_scoped_logger(self, mgr):
        logger = mgr.get_logger("host1", "my-plugin")
        assert logger.target == "host1"
        assert logger.plugin_name == "my-plugin"

    def test_write_inserts_prefixed_event(self, mgr):
        logger = mgr.get_logger("host1", "test-plugin")
        logger.write("something happened", level="WARNING")
        with db.connection_context():
            e = Event.select().where(Event.level == "WARNING").first()
        assert e is not None
        assert "[test-plugin] something happened" in e.message
        assert e.target == "host1"

    def test_metric_inserts_metric_row(self, mgr):
        logger = mgr.get_logger("host1", "test-plugin")
        logger.metric("cpu_pct", 42.5)
        with db.connection_context():
            m = Metric.select().where(
                (Metric.collector == "test-plugin") & (Metric.metric_name == "cpu_pct")
            ).first()
        assert m is not None
        assert m.value == pytest.approx(42.5)
        assert m.target == "host1"
