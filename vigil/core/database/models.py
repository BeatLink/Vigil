"""Peewee ORM models for the Database Engine.

The SQLite handle (`db`) and every table model live here, separated from the
read/write logic in `database.py`. `database.py` re-exports these names, so
`from vigil.core.database.database import Metric, db, ...` continues to work.
"""

from datetime import datetime
from peewee import (
    CharField,
    DateTimeField,
    DoubleField,
    ForeignKeyField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

db = SqliteDatabase(None)


class BaseModel(Model):
    class Meta:
        database = db


class Metric(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    collector = CharField()
    metric_name = CharField(index=True)
    value = DoubleField()
    metadata = TextField(null=True)

    class Meta:
        # The hot read path filters on (collector, metric_name) and orders by
        # timestamp DESC (latest_metric_cached / metric_history_cached), and
        # collector_metrics_cached filters on collector alone — both served by
        # this composite (the leading `collector` also covers the
        # collector-only prefix). Without it those queries scan the ever-growing
        # Metric table. See database._migrate() for the same index on existing DBs.
        indexes = ((("collector", "metric_name", "timestamp"), False),)


class Event(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    level = CharField()
    message = TextField()
    target = CharField(null=True)
    source_id = CharField(null=True, index=True)


class Setting(BaseModel):
    key = CharField(primary_key=True)
    value = TextField()


class StatusHistory(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    collector_id = CharField(index=True)
    state = CharField()


class Job(BaseModel):
    plugin_id = CharField(index=True)
    target = CharField(index=True)
    kind = CharField(index=True)
    state = CharField(index=True, default="running")
    command = TextField()
    started = DateTimeField(default=datetime.now, index=True)
    finished = DateTimeField(null=True)
    exit_code = IntegerField(null=True)
    progress = TextField(null=True)
    error = TextField(null=True)
    # Detached-on-target execution: the remote PID and working dir let a poll
    # (a plain SSH command) check liveness / read the exit file, and let a
    # restarted Vigil re-adopt a job that is still running on the target.
    pid = IntegerField(null=True)
    workdir = TextField(null=True)
    output_seq = IntegerField(default=0)


class JobOutput(BaseModel):
    job = ForeignKeyField(Job, backref="output", on_delete="CASCADE", index=True)
    seq = IntegerField()
    timestamp = DateTimeField(default=datetime.now)
    stream = CharField(default="stdout")
    message = TextField()

    class Meta:
        indexes = ((("job", "seq"), True),)


class PluginSnapshot(BaseModel):
    plugin_id = CharField(primary_key=True)
    updated = DateTimeField(default=datetime.now)
    data = TextField()


class LogLine(BaseModel):
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    source = CharField(index=True)
    level = CharField()
    message = TextField()
    dedup_hash = CharField(unique=True)


ALL_MODELS = [
    Metric,
    Event,
    Setting,
    StatusHistory,
    Job,
    JobOutput,
    PluginSnapshot,
    LogLine,
]
