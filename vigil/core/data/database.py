import logging
from datetime import datetime
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
            db.create_tables([Metric, Event, Setting])
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

    def insert_event(self, level: str, message: str, target: Optional[str] = None):
        """Inserts a new event record."""
        with db.connection_context():
            Event.create(level=level, message=message, target=target)
            logging.debug(f"Inserted event: {level} - {message}")

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
