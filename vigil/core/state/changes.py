"""Change bus — the notification half of the State Engine.

The store holds what is true; this says when it changed. Every semantic write
on the Database Engine publishes one change here, tagged with its kind and the
monitor it belongs to, and subscribers (the dashboard's per-client schedulers)
refresh off that instead of polling on a timer.

Publishing is synchronous and happens on whatever thread performed the write —
a collector's worker thread, the agent's socket task, the UI's own loop. A
subscriber therefore does the minimum possible inline and marshals the real
work onto its own event loop; nothing here awaits, locks for long, or lets a
subscriber's exception escape into the write path.
"""

import logging
import threading
from typing import Callable, Optional

# Change kinds, one per datatype the store holds.
STATUS = "status"
METRIC = "metric"
EVENT = "event"
LOG = "log"
JOB = "job"
SNAPSHOT = "snapshot"
SETTING = "setting"

# A subscriber takes (kind, plugin_id); plugin_id is None for changes that
# belong to no single monitor, such as a global setting.
Subscriber = Callable[[str, Optional[str]], None]


class ChangeBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: list = []

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """Register `subscriber` and return the callable that removes it."""
        with self._lock:
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def publish(self, kind: str, plugin_id: Optional[str] = None) -> None:
        """Announce one change. Callable from any thread."""
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            # One failing subscriber must not poison the bus for the others or the publisher.
            try:
                subscriber(kind, plugin_id)
            except Exception as e:
                logging.error(f"change subscriber failed for {kind!r}: {e}")


# One bus per process: the Database Engine publishes to it and the dashboard
# subscribes, without either having to hold a reference to the other.
CHANGES = ChangeBus()
