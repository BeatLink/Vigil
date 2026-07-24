import pytest
from unittest.mock import AsyncMock
from typing import List

from vigil.collector.collector_plugin_base import CollectorPlugin
from vigil.collector.orchestration.types import CmdResult, Command, CollectResult
from vigil.core.data.database import db, Event, Metric


class _Probe(CollectorPlugin):
    def commands(self) -> List[Command]:
        return []

    def parse(self, results: List[CmdResult]) -> CollectResult:
        return CollectResult()


@pytest.fixture
def colliding(make_plugin):
    a = make_plugin(_Probe, {"name": "On Disk", "id": "odin-borgmatic-on-disk"})
    b = make_plugin(_Probe, {"name": "On Disk", "id": "heimdall-borgmatic-on-disk"})
    return a, b


class TestMetricScoping:
    def test_metrics_are_written_under_the_id(self, colliding, db_manager):
        a, _ = colliding
        a.storage.apply(CollectResult(metrics={"last_backup_epoch": 111.0}))
        db_manager.flush()
        with db.connection_context():
            row = Metric.select().where(Metric.metric_name == "last_backup_epoch").first()
        assert row.collector == "odin-borgmatic-on-disk"

    def test_each_monitor_reads_its_own_metric(self, colliding, db_manager):
        a, b = colliding
        a.storage.apply(CollectResult(metrics={"last_backup_epoch": 111.0}))
        b.storage.apply(CollectResult(metrics={"last_backup_epoch": 222.0}))
        db_manager.flush()

        assert a.storage.latest_metric("last_backup_epoch").value == 111.0
        assert b.storage.latest_metric("last_backup_epoch").value == 222.0

    def test_sibling_writes_do_not_leak(self, colliding, db_manager):
        a, b = colliding
        b.storage.apply(CollectResult(metrics={"archive_count": 9.0}))
        db_manager.flush()
        assert a.storage.latest_metric("archive_count") is None


class TestLogLineScoping:
    def test_log_lines_are_written_under_the_id(self, colliding, db_manager):
        from vigil.core.data.database import LogLine
        a, _ = colliding
        a.storage.apply(CollectResult(log_lines=[("boot ok", "INFO", "2026-01-01T00:00:00")]))
        db_manager.flush()
        with db.connection_context():
            row = LogLine.select().first()
        assert row.source == "odin-borgmatic-on-disk"

    def test_identical_lines_from_siblings_both_survive(self, colliding, db_manager):
        from vigil.core.data.database import LogLine
        a, b = colliding
        for p in (a, b):
            p.storage.apply(CollectResult(log_lines=[
                ("Started nixos-upgrade.service", "INFO", "2026-01-01T00:00:00"),
            ]))
        db_manager.flush()
        with db.connection_context():
            sources = {r.source for r in LogLine.select()}
        assert sources == {"odin-borgmatic-on-disk", "heimdall-borgmatic-on-disk"}


class TestDuplicateIdDetection:
    def _engine(self, tmp_path, plugins):
        from unittest.mock import patch
        from vigil.collector.main import VigilEngine
        cfg = tmp_path / "c.yaml"
        cfg.write_text("plugins: []\n")
        with patch("vigil.collector.main.VigilEngine._connect", create=True):
            engine = VigilEngine(str(cfg), db_path_override=str(tmp_path / "e.db"))
        engine.plugins = plugins
        return engine

    def test_reports_monitors_sharing_an_id(self, tmp_path, colliding, caplog):
        a, b = colliding
        b.id = a.id
        engine = self._engine(tmp_path, [a, b])
        with caplog.at_level('ERROR'):
            engine._warn_on_duplicate_ids()
        assert any("Duplicate monitor id" in r.message for r in caplog.records)

    def test_silent_when_ids_are_unique(self, tmp_path, colliding, caplog):
        a, b = colliding
        engine = self._engine(tmp_path, [a, b])
        with caplog.at_level('ERROR'):
            engine._warn_on_duplicate_ids()
        assert not any("Duplicate monitor id" in r.message for r in caplog.records)

    def test_checks_nested_children(self, tmp_path, colliding, caplog):
        a, b = colliding
        b.id = a.id
        parent = a
        parent.children = [b]
        engine = self._engine(tmp_path, [parent])
        with caplog.at_level('ERROR'):
            engine._warn_on_duplicate_ids()
        assert any("Duplicate monitor id" in r.message for r in caplog.records)


class TestMigration:
    def test_adds_source_id_to_a_pre_existing_event_table(self, tmp_path):
        import sqlite3
        from vigil.core.data.database import DatabaseManager, db as peewee_db

        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, timestamp DATETIME, "
                    "level VARCHAR(255), message TEXT, target VARCHAR(255))")
        old.commit()
        old.close()

        if not peewee_db.is_closed():
            peewee_db.close()
        DatabaseManager(str(path))

        with peewee_db.connection_context():
            cols = {c.name for c in peewee_db.get_columns('event')}
        assert 'source_id' in cols
        if not peewee_db.is_closed():
            peewee_db.close()

    def test_migration_is_idempotent(self, tmp_path):
        from vigil.core.data.database import DatabaseManager, db as peewee_db
        path = tmp_path / "twice.db"
        if not peewee_db.is_closed():
            peewee_db.close()
        DatabaseManager(str(path))
        if not peewee_db.is_closed():
            peewee_db.close()
        DatabaseManager(str(path))
        if not peewee_db.is_closed():
            peewee_db.close()


class TestEventScoping:
    def test_events_record_the_source_id(self, colliding, db_manager):
        a, _ = colliding
        a.storage.apply(CollectResult(logs=[("hello", "INFO")]))
        db_manager.flush()
        with db.connection_context():
            row = Event.select().where(Event.message.contains("hello")).first()
        assert row.source_id == "odin-borgmatic-on-disk"

    def test_event_prefix_keeps_the_display_name(self, colliding, db_manager):
        a, _ = colliding
        a.storage.apply(CollectResult(logs=[("hello", "INFO")]))
        db_manager.flush()
        with db.connection_context():
            row = Event.select().where(Event.message.contains("hello")).first()
        assert row.message.startswith("[On Disk] ")

    def test_events_are_separable_by_id(self, colliding, db_manager):
        a, b = colliding
        a.storage.apply(CollectResult(logs=[("from a", "INFO")]))
        b.storage.apply(CollectResult(logs=[("from b", "INFO")]))
        db_manager.flush()
        with db.connection_context():
            got = [e.message for e in
                   Event.select().where(Event.source_id == "heimdall-borgmatic-on-disk")]
        assert len(got) == 1 and "from b" in got[0]
