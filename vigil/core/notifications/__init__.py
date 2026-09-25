"""Notification Engine.

Watches every monitor's status writes on the change bus and sends a
notification when a monitor's rule says the change matters: a new problem,
a reminder while it lasts, or its recovery. See docs/notifications.md.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Set
from urllib.parse import quote

from vigil.core.notifications.channels import Channel, DesktopChannel, Message, digest
from vigil.core.notifications.http import NtfyChannel, WebhookChannel
from vigil.core.notifications.mail import AppriseChannel, SmtpChannel
from vigil.core.notifications.maintenance import Window, parse_windows
from vigil.core.notifications.rules import (
    FLAPPING, PROBLEM, RECOVERED, SETTLED, Alert, Tracker, resolve_rules,
)
from vigil.core.state import changes
from vigil.core.state.changes import CHANGES
from vigil.plugins.base.plugin_helpers import format_duration, parse_duration

RETRY_DELAYS = (5, 30)
"""Seconds to wait before each retry of a failed delivery."""

MUTE_SETTING = "notifications.muted:{}"
"""The setting that mutes one monitor, and everything beneath it when it is a group."""


CHANNEL_TYPES = {cls.TYPE: cls for cls in (DesktopChannel, WebhookChannel, NtfyChannel,
                                           SmtpChannel, AppriseChannel)}


def build_channels(entries: List[Dict[str, Any]], agents: Any) -> Dict[str, Channel]:
    """The configured channels by id. A bad entry is logged and skipped."""
    channels: Dict[str, Channel] = {}
    for entry in entries or []:
        channel_id = str(entry.get('id') or entry.get('type') or '')
        if not channel_id or channel_id in channels:
            logging.error(f"notifications: channel needs a unique `id`, skipping {entry!r}")
            continue
        channel_type = CHANNEL_TYPES.get(entry.get('type'))
        if channel_type is None:
            logging.error(f"notifications: channel {channel_id!r} has unknown type {entry.get('type')!r}")
            continue
        # A broken channel must not stop the others from working.
        try:
            channels[channel_id] = channel_type(channel_id, entry, agents)
        except ValueError as e:
            logging.error(f"notifications: channel {channel_id!r} disabled: {e}")
    return channels


def monitor_url(base_url: Optional[str], monitor_id: str) -> Optional[str]:
    """The dashboard link to one monitor's page, when the dashboard's address is configured."""
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/monitor/{quote(monitor_id, safe='')}"


class NotificationEngine:
    """Owns the notification channels and decides when to use them."""

    def __init__(self, db: Any, settings: Optional[Mapping[str, Any]], agents: Any):
        self._db = db
        settings = settings or {}
        self.base_url = settings.get('base_url')
        self.channels: Dict[str, Channel] = build_channels(settings.get('channels') or [], agents)
        self._defaults = settings.get('defaults') or {}
        self.group_window = parse_duration(settings.get('group_window', 0) or 0)
        self.windows: List[Window] = parse_windows(settings.get('maintenance') or [])
        self._held: Set[str] = set()
        self._pending: Dict[str, List[Message]] = {}
        self._monitors: Dict[str, Any] = {}
        self._parents: Dict[str, str] = {}
        self._announced: Set[str] = set()
        self._tracker = Tracker({})
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._unsubscribe = None
        self._tasks: Set[asyncio.Task] = set()

    def start(self, roots: List[Any]) -> None:
        """Resolve each monitor's rule and start watching status writes. Needs the running loop."""
        if not self.channels:
            return
        rules = resolve_rules(roots, self._defaults, list(self.channels))
        self._tracker = Tracker(rules)
        stack = list(roots)
        while stack:
            plugin = stack.pop()
            for child in plugin.children:
                self._parents[child.id] = plugin.id
            stack.extend(plugin.children)
            self._monitors[plugin.id] = plugin

        now = time.time()
        for plugin_id, record in self._db.store.statuses.items():
            self._tracker.seed(plugin_id, record.state, record.timestamp.timestamp(), now)
        self._announced = {pid for pid in rules if self._tracker.alerting(pid)}

        self._loop = asyncio.get_running_loop()
        self._unsubscribe = CHANGES.subscribe(self._on_change)
        logging.info(
            f"Notifications: {len(self.channels)} channel(s), {len(rules)} monitor(s) notify"
        )

    def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        for task in list(self._tasks):
            task.cancel()

    def _on_change(self, kind: str, plugin_id: Optional[str]) -> None:
        """Change-bus subscriber: runs on the writer's thread, so it only hands off."""
        if kind != changes.STATUS or plugin_id not in self._tracker.rules:
            return
        status = self._db.latest_status(plugin_id)
        try:
            self._loop.call_soon_threadsafe(self._observe, plugin_id, status, time.time())
        except RuntimeError:
            pass  # The loop has closed during shutdown.

    def monitor_name(self, plugin_id: str) -> str:
        """A monitor's display name, falling back to its id."""
        return getattr(self._monitors.get(plugin_id), 'name', plugin_id)

    def muted_by(self, plugin_id: str) -> Optional[str]:
        """The monitor or group whose mute silences this monitor, or None."""
        current: Optional[str] = plugin_id
        while current is not None:
            if self._db.get_setting(MUTE_SETTING.format(current)) == "1":
                return current
            current = self._parents.get(current)
        return None

    def _lineage(self, plugin_id: str) -> List[str]:
        """A monitor's id followed by the ids of the groups it sits in."""
        ids, current = [], plugin_id
        while current is not None:
            ids.append(current)
            current = self._parents.get(current)
        return ids

    def maintenance_for(self, plugin_id: str, now: Optional[datetime] = None) -> Optional[str]:
        """The name of the maintenance window covering this monitor right now, or None."""
        now = now or datetime.now()
        lineage = self._lineage(plugin_id)
        for window in self.windows:
            if window.covers(lineage) and window.active(now):
                return window.name
        return None

    def active_windows(self, now: Optional[datetime] = None) -> List[Window]:
        """The maintenance windows in effect right now."""
        now = now or datetime.now()
        return [w for w in self.windows if w.active(now)]

    def set_muted(self, plugin_id: str, muted: bool) -> None:
        """Mute or unmute one monitor, or a group and everything beneath it."""
        self._db.set_setting(MUTE_SETTING.format(plugin_id), "1" if muted else "0")

    def muted_monitors(self) -> List[Any]:
        """The monitors and groups muted directly, in no particular order."""
        return [plugin for plugin_id, plugin in self._monitors.items()
                if self._db.get_setting(MUTE_SETTING.format(plugin_id)) == "1"]

    def _observe(self, plugin_id: str, status: str, now: float) -> None:
        alert = self._tracker.observe(plugin_id, status, now)
        in_maintenance = self.maintenance_for(plugin_id, datetime.fromtimestamp(now)) is not None
        if alert is None and plugin_id in self._held and not in_maintenance:
            # A problem held back by a maintenance window is announced once the window is over.
            self._held.discard(plugin_id)
            if plugin_id not in self._announced and self.muted_by(plugin_id) is None:
                alert = self._tracker.problem(plugin_id)
        if alert is None:
            return
        if in_maintenance and alert.kind != RECOVERED:
            self._held.add(plugin_id)
        muted = self.muted_by(plugin_id) is not None or in_maintenance
        channels = [self.channels[c] for c in self._tracker.rules[plugin_id].channels]
        if alert.kind == RECOVERED:
            announced = plugin_id in self._announced
            self._announced.discard(plugin_id)
            # A recovery is only news if its problem was announced; muted, it only clears that notification.
            if not announced:
                return
            if muted:
                channels = [c for c in channels if getattr(c, 'dismisses_on_recovery', False)]
        elif muted:
            return
        else:
            self._announced.add(plugin_id)
        message = self.message(plugin_id, alert, now)
        for channel in channels:
            self._enqueue(channel, message)

    def _enqueue(self, channel: Channel, message: Message) -> None:
        """Send now, or hold for the group window so notifications arriving together go as one."""
        if not self.group_window:
            self._spawn(self._deliver(channel, [message]))
            return
        pending = self._pending.setdefault(channel.id, [])
        pending.append(message)
        if len(pending) == 1:
            asyncio.get_running_loop().call_later(self.group_window, self._flush, channel)

    def _flush(self, channel: Channel) -> None:
        messages = self._pending.pop(channel.id, [])
        if messages:
            self._spawn(self._deliver(channel, messages))

    def message(self, plugin_id: str, alert: Alert, now: float) -> Message:
        """The notification for one alert on one monitor."""
        plugin = self._monitors.get(plugin_id)
        name = getattr(plugin, 'name', plugin_id)
        status = alert.status
        if alert.kind == RECOVERED and alert.settled:
            title = f"{name} stopped flapping and recovered" if status == 'online' else f"{name} stopped flapping and is {status}"
            detail = ""
        elif alert.kind == RECOVERED:
            title = f"{name} recovered" if status == 'online' else f"{name} is {status}"
            detail = f"Was down for {format_duration(int(now - alert.since))}"
        elif alert.kind == FLAPPING:
            window = format_duration(self._tracker.rules[plugin_id].flap_window)
            title = f"{name} is flapping"
            detail = f"Its status keeps changing, so nothing more is sent until it holds steady for {window}."
        elif alert.kind == SETTLED:
            title = f"{name} stopped flapping and is {status}"
            detail = self._reason(plugin_id, name)
        else:
            title = f"{name} is {status}" if alert.kind == PROBLEM else f"{name} is still {status}"
            detail = self._reason(plugin_id, name)
        target = getattr(plugin, 'target', '') or ''
        body = "\n".join(line for line in (detail, f"Host: {target}" if target else "") if line)
        return Message(title, body, alert.status, alert.kind, plugin_id,
                       monitor_url(self.base_url, plugin_id), name, target, now)

    def _reason(self, plugin_id: str, name: str) -> str:
        """The monitor's latest event, which says why its status changed."""
        events = self._db.store.plugin_events(plugin_id=plugin_id, limit=1)
        if not events:
            return ""
        return events[0].message.removeprefix(f"[{name}] ")

    async def send_test(self, channel_id: str) -> None:
        """Send a test notification through one channel, raising if it fails."""
        channel = self.channels.get(channel_id)
        if channel is None:
            raise KeyError(channel_id)
        await channel.send(Message(
            "Vigil test notification", f"Sent through the {channel_id!r} channel.",
            "online", "test", url=self.base_url, timestamp=time.time(),
        ))

    async def _deliver(self, channel: Channel, messages: List[Message]) -> None:
        """Send one notification, or several as one batch, retrying before reporting a failure."""
        if len(messages) == 1:
            summary = messages[0]
        else:
            summary = digest(messages, self.base_url)
        for attempt, delay in enumerate((0, *RETRY_DELAYS)):
            await asyncio.sleep(delay)
            # Any failure is retried, then reported; a broken channel must not stop the others.
            try:
                if len(messages) == 1:
                    await channel.send(summary)
                else:
                    await channel.send_batch(messages, summary)
                logging.info(f"Notified {channel.id!r}: {summary.title}")
                return
            except Exception as e:
                error = e
                logging.warning(f"Notification to {channel.id!r} failed (try {attempt + 1}): {e}")
        self._db.insert_event(
            "WARNING",
            f"[notifications] Could not notify {channel.id!r} that {summary.title}: {error}",
            "vigil_core", plugin_id=summary.monitor_id or None,
        )

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
