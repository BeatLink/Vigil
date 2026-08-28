"""State Engine — the in-memory system of record.

Collectors write here and the UI reads from here; SQLite is a persistence
sink behind it, not a read path. See ``store.py``. ``changes.py`` carries the
matching notifications, so the UI refreshes on write instead of on a timer.
"""

from vigil.core.state.changes import CHANGES, ChangeBus
from vigil.core.state.records import (
    EventRecord,
    JobRecord,
    JobOutputRecord,
    LogLineRecord,
    MetricRecord,
    StatusRecord,
)
from vigil.core.state.store import BufferSizes, StateStore

__all__ = [
    "BufferSizes",
    "CHANGES",
    "ChangeBus",
    "EventRecord",
    "JobRecord",
    "JobOutputRecord",
    "LogLineRecord",
    "MetricRecord",
    "StateStore",
    "StatusRecord",
]
