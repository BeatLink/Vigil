from peewee import *
from datetime import datetime

# Initialize a Peewee database instance
# This will be bound to the actual file path later in VigilDatabase __init__
db = SqliteDatabase(None)

class BaseModel(Model):
    class Meta:
        database = db

class Metric(BaseModel):
    """
    Model for storing collected metrics.
    """
    timestamp = DateTimeField(default=datetime.now, index=True)
    target = CharField(index=True)
    collector = CharField()
    metric_name = CharField(index=True)
    value = DoubleField()
    metadata = TextField(null=True) # For storing JSON or other structured data

class Event(BaseModel):
    """
    Model for storing system events and logs.
    """
    timestamp = DateTimeField(default=datetime.now, index=True)
    level = CharField()
    message = TextField()
    target = CharField(null=True)

class Setting(BaseModel):
    """
    Model for storing persistent key-value settings.
    """
    key = CharField(primary_key=True)
    value = TextField()