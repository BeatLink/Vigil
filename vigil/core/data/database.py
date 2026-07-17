import logging
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Any
from peewee import *

# Initialize a Peewee database instance
db = SqliteDatabase(None)

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

    def log_line(self, message: str, level: str = "INFO", log_time: Optional[str] = None) -> bool:
        """
        Persist a raw log line collected from the target, deduplicated.

        `log_time` should be the line's own timestamp (e.g. the journald
        timestamp) when available — it makes the dedup identity stable across
        cycles so the same line is stored exactly once. Returns True if the line
        was newly stored, False if it was a duplicate.
        """
        return self.db.insert_log_line(self.target, self.plugin_name, level, message, log_time)

# DATABASE MANAGER ##################################################################################################################################
class DatabaseManager:
    """Manages the Peewee ORM connection and schema for Vigil."""
    def __init__(self, db_path: str = "vigil.db"):
        self.db_path = db_path
        self._connect_and_init()

    def _connect_and_init(self):
        """Connects to the database and creates tables if they don't exist."""
        try:
            db.init(self.db_path)
            db.connect()
            db.create_tables([Metric, Event, Setting, StatusHistory, LogLine])
            logging.info(f"Database initialized and connected at {self.db_path}")
        except OperationalError as e:
            logging.error(f"Failed to connect or initialize database at {self.db_path}: {e}")
            raise
        finally:
            if not db.is_closed():
                db.close()

    def insert_metric(self, target: str, collector: str, metric_name: str, value: float, metadata: Optional[str] = None):
        """Inserts a new metric record."""
        with db.connection_context():
            Metric.create(target=target, collector=collector, metric_name=metric_name, value=value, metadata=metadata)
            logging.debug(f"Inserted metric: {metric_name}={value} for {target}")

    def insert_status(self, collector_id: str, state: str):
        """Inserts a new status history record."""
        with db.connection_context():
            StatusHistory.create(collector_id=collector_id, state=state)
            logging.debug(f"Recorded status for {collector_id}: {state}")

    def insert_event(self, level: str, message: str, target: Optional[str] = None):
        """Inserts a new event record."""
        with db.connection_context():
            Event.create(level=level, message=message, target=target) # Ensure target is passed
            logging.debug(f"Inserted event: {level} - {message}")

    def insert_log_line(self, target: str, source: str, level: str, message: str,
                        log_time: Optional[str] = None) -> bool:
        """
        Insert a raw log line, deduplicated by content.

        The dedup key is a hash of (target, source, log_time, message). Because
        collectors re-fetch the same trailing lines each cycle, an INSERT that
        collides on the UNIQUE dedup_hash is ignored rather than duplicated.

        Returns True if a new row was written, False if it was already present.
        """
        # log_time anchors the identity to the line's own timestamp when the
        # source provides one; without it we fall back to (target, source, msg)
        # so identical repeated lines still collapse to a single row.
        key = f"{target}\x1f{source}\x1f{log_time or ''}\x1f{message}"
        dedup_hash = hashlib.sha1(key.encode('utf-8', 'replace')).hexdigest()
        with db.connection_context():
            # The UNIQUE constraint on dedup_hash is what actually guarantees
            # dedup; on_conflict_ignore turns a re-insert into a no-op. We check
            # existence first only to report whether the line was newly stored.
            existed = LogLine.select().where(LogLine.dedup_hash == dedup_hash).exists()
            if not existed:
                (LogLine
                 .insert(target=target, source=source, level=level,
                         message=message, dedup_hash=dedup_hash)
                 .on_conflict_ignore()
                 .execute())
        return not existed

    def prune_logs(self, retention_days: int) -> int:
        """
        Delete stored log lines older than `retention_days`. A value <= 0
        disables pruning (logs are kept indefinitely). Returns rows deleted.
        """
        if retention_days is None or retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=retention_days)
        with db.connection_context():
            deleted = LogLine.delete().where(LogLine.timestamp < cutoff).execute()
        if deleted:
            logging.info(f"Pruned {deleted} log line(s) older than {retention_days}d")
        return deleted

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieves a runtime setting from the database."""
        with db.connection_context():
            try:
                return Setting.get(Setting.key == key).value
            except DoesNotExist:
                return default

    def set_setting(self, key: str, value: str):
        """Sets or updates a runtime setting in the database."""
        with db.connection_context():
            Setting.insert(key=key, value=value).on_conflict_replace().execute()
            logging.debug(f"Updated setting: {key}")

    def get_logger(self, target: str, plugin_name: str) -> InternalDatabaseLogger:
        """Factory method to provide a scoped logger for a specific plugin instance."""
        return InternalDatabaseLogger(self, target, plugin_name)
