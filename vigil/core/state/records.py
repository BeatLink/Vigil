"""In-memory record types held by the StateStore.

These replace the peewee model instances that used to be handed to the UI.
Callers read them by attribute (``m.value``, ``m.timestamp``) exactly as they
read peewee models, so the UI's readers are unchanged by the move off SQLite.

Every record is a frozen dataclass with ``slots=True``: they are created on
every poll of every plugin and held in bounded ring buffers, so the per-record
overhead matters, and immutability means a reader that captured a record can
never observe it mutating underneath a concurrent collector write.

``as_row()`` renders the dict shape that the DB read methods used to return
from peewee's ``Model.__data__``, keeping the UI's table readers (which index
rows by column name) working against the store.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True, slots=True)
class MetricRecord:
    target: str
    plugin_id: str
    metric_name: str
    value: float
    metadata: Optional[str] = None
    timestamp: datetime = None  # type: ignore[assignment]

    def as_row(self) -> Dict[str, Any]:
        """The ``Metric.__data__`` shape the metric tables render. ``id`` is
        absent — nothing reads it, and in-memory records have no rowid."""
        return {
            "timestamp": self.timestamp,
            "target": self.target,
            "plugin_id": self.plugin_id,
            "metric_name": self.metric_name,
            "value": self.value,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    level: str
    message: str
    target: Optional[str] = None
    plugin_id: Optional[str] = None
    timestamp: datetime = None  # type: ignore[assignment]

    def as_row(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "message": self.message,
            "target": self.target,
            "plugin_id": self.plugin_id,
        }


@dataclass(frozen=True, slots=True)
class LogLineRecord:
    target: str
    plugin_id: str
    level: str
    message: str
    dedup_hash: str
    timestamp: datetime = None  # type: ignore[assignment]

    def as_row(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "target": self.target,
            "plugin_id": self.plugin_id,
            "level": self.level,
            "message": self.message,
            "dedup_hash": self.dedup_hash,
        }


@dataclass(frozen=True, slots=True)
class StatusRecord:
    plugin_id: str
    state: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class JobOutputRecord:
    seq: int
    stream: str
    message: str
    timestamp: datetime


@dataclass(slots=True)
class JobRecord:
    """The one mutable record. A job is advanced in place (pid, progress,
    state, exit_code) by the owning plugin's successive polls, and readers
    take a rendered dict via ``as_dict()`` rather than holding the record, so
    in-place mutation is never observed mid-update by the UI."""

    id: int
    plugin_id: str
    target: str
    kind: str
    state: str
    command: str
    started: datetime
    finished: Optional[datetime] = None
    exit_code: Optional[int] = None
    progress: Optional[str] = None
    error: Optional[str] = None
    pid: Optional[int] = None
    workdir: Optional[str] = None
    output_seq: int = 0

    def as_dict(self) -> Dict[str, Any]:
        """The ``JobDict`` shape (see database/rowtypes.py) the job panel
        renders. ``duration`` is computed against now() while running, so it
        ticks up in the UI without the record being touched."""
        end = self.finished or datetime.now()
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "target": self.target,
            "kind": self.kind,
            "state": self.state,
            "command": self.command,
            "started": self.started.isoformat(sep=" ", timespec="seconds"),
            "finished": (
                self.finished.isoformat(sep=" ", timespec="seconds")
                if self.finished
                else None
            ),
            "duration": max(0, int((end - self.started).total_seconds())),
            "exit_code": self.exit_code,
            "progress": self.progress,
            "error": self.error,
            "running": self.state == "running",
            "pid": self.pid,
            "workdir": self.workdir,
            "output_seq": self.output_seq or 0,
        }
