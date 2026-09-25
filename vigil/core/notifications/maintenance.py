"""Maintenance windows: times when chosen monitors send no problem notifications.

A window recurs daily between two times of day, optionally on chosen weekdays, or runs once
between two dates. Times are the server's local time. A problem that begins during a window
and is still there when it ends is announced then.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, FrozenSet, List, Optional

DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


@dataclass(frozen=True)
class Window:
    """One maintenance window."""
    name: str
    monitors: Optional[FrozenSet[str]] = None
    days: Optional[FrozenSet[int]] = None
    daily_from: Optional[time] = None
    daily_to: Optional[time] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    def active(self, now: datetime) -> bool:
        """Whether the window is in effect at `now`."""
        if self.start is not None:
            return self.start <= now < self.end
        moment = now.time()
        if self.daily_from <= self.daily_to:
            return self.daily_from <= moment < self.daily_to and self._on(now)
        # A window past midnight belongs to the day it started on.
        if moment >= self.daily_from:
            return self._on(now)
        return moment < self.daily_to and self._on(now - timedelta(days=1))

    def _on(self, day: datetime) -> bool:
        return self.days is None or day.weekday() in self.days

    def describe(self) -> str:
        """The window's schedule in words, such as "Sun 03:00-05:00"."""
        if self.start is not None:
            return f"{self.start:%Y-%m-%d %H:%M} to {self.end:%Y-%m-%d %H:%M}"
        days = "Daily" if self.days is None else ", ".join(DAYS[d].title() for d in sorted(self.days))
        return f"{days} {self.daily_from:%H:%M}-{self.daily_to:%H:%M}"

    def covers(self, monitor_ids: List[str]) -> bool:
        """Whether the window applies to a monitor, given its id followed by its groups' ids."""
        return self.monitors is None or any(m in self.monitors for m in monitor_ids)


def _time(value: Any, key: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as e:
        raise ValueError(f"`{key}` must be a time of day such as \"03:00\", not {value!r}") from e


def _datetime(value: Any, key: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as e:
        raise ValueError(f"`{key}` must be a date and time such as 2026-10-01T09:00, not {value!r}") from e


def window(entry: Dict[str, Any]) -> Window:
    """Build one window from its config entry, raising ValueError if it is malformed."""
    name = str(entry.get('name') or 'maintenance')
    monitors = entry.get('monitors')
    if isinstance(monitors, str):
        monitors = [monitors]
    covered = frozenset(str(m) for m in monitors) if monitors else None

    if entry.get('start') is not None or entry.get('end') is not None:
        start, end = _datetime(entry.get('start'), 'start'), _datetime(entry.get('end'), 'end')
        if end <= start:
            raise ValueError("`end` must come after `start`")
        return Window(name, covered, start=start, end=end)

    if entry.get('from') is None or entry.get('to') is None:
        raise ValueError("give `from` and `to` for a daily window, or `start` and `end` for a one-off one")
    days = entry.get('days')
    if isinstance(days, str):
        days = [days]
    weekdays = None
    if days:
        unknown = [d for d in days if str(d).lower()[:3] not in DAYS]
        if unknown:
            raise ValueError(f"`days` must be weekday names such as mon or tuesday, not {unknown}")
        weekdays = frozenset(DAYS.index(str(d).lower()[:3]) for d in days)
    daily_from, daily_to = _time(entry['from'], 'from'), _time(entry['to'], 'to')
    if daily_from == daily_to:
        raise ValueError("`from` and `to` must differ")
    return Window(name, covered, weekdays, daily_from, daily_to)


def parse_windows(entries: List[Dict[str, Any]]) -> List[Window]:
    """Every well-formed window; a malformed one is logged and left out."""
    windows = []
    for entry in entries or []:
        # One bad window must not stop the others from applying.
        try:
            windows.append(window(entry))
        except (ValueError, TypeError, AttributeError) as e:
            label = entry.get('name', entry) if isinstance(entry, dict) else entry
            logging.error(f"notifications: maintenance window {label!r} ignored: {e}")
    return windows
